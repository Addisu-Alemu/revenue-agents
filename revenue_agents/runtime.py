"""Agent runtime: each agent is a model working in a loop with its own small set of tools.

    task -> the model picks a tool -> PreToolUse hook asks the policy -> allow: the tool runs (a side effect
                                                                         still goes through the gateway)
                                                                      -> defer: the run stops, the call waits
                                                                         in the approval inbox for a human
                                                                      -> deny:  the reason goes back to the model
         -> ... until the model says it is done

The hook only answers "may this call run now?". The gateway still decides and
executes every side effect (claims, audit, approvals), so the hook is a
second layer, not the only one. Action ids come from the tool input (same
deal, same step, same id), never from the model, so a retried event can't
send twice.

Approving a deferred call runs the stored action through the gateway; the
model is not resumed. A rejection with a note starts a new run with the note.

Two runtimes play the same agents with the same tools, guard and gateway:
- ClaudeRuntime: the real model, through the Claude Agent SDK. Tools are an
  in-process MCP server; no built-in tools (files, shell, web), no personal
  settings, only this agent's tools allowed, and a turn limit and spend cap
  per run.
- ScriptedRuntime: each agent's scripted decisions, for tests and the offline
  demo. Its answers come from the scripted answer bank (demo_llm.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError

from revenue_agents.guardrails.gateway import ActionGateway, ActionResult, Outcome
from revenue_agents.guardrails.policy import Action, ActionType, Decision
from revenue_agents.llm import DEFAULT_MODEL, LLM, LLMError
from revenue_agents.models import Model

if TYPE_CHECKING:
    from revenue_agents.crm import CRM
    from revenue_agents.tracing import Tracer

T = TypeVar("T", bound=BaseModel)

SERVER = "desk"  # the in-process MCP server's name: the model sees tools as mcp__desk__<tool>
TOOL_PREFIX = f"mcp__{SERVER}__"


class NoInput(Model):
    """For tools that take no arguments."""


class ToolResult(Model):
    """What the model reads back after a tool call."""

    text: str
    data: dict[str, Any] = Field(default_factory=dict)
    is_error: bool = False


class Verdict(str, Enum):
    ALLOW = "allow"
    DEFER = "defer"
    DENY = "deny"


class Permission(Model):
    """The PreToolUse hook's answer for one call, and the rule behind it."""

    verdict: Verdict
    rule: str
    reason: str


@dataclass(frozen=True)
class Tool:
    """One tool an agent may call. Read tools have `run`; side-effect tools have `action`.

    - run(args): read something or compute something. No side effects.
    - action(args): the action this call proposes (always of type `proposes`). It runs only through the gateway.
    - done(args, result): what the model is told after the action ran (optional).
    - check(args): a rule in code that can refuse the call, e.g. "no drafted answer to a pricing question".
    """

    name: str
    description: str
    input: type[BaseModel]
    run: Callable[[Any], ToolResult] | None = None
    action: Callable[[Any], Action] | None = None
    done: Callable[[Any, ActionResult], ToolResult] | None = None
    check: Callable[[Any], str | None] | None = None
    proposes: ActionType | None = None

    @property
    def effect(self) -> bool:
        return self.action is not None


@dataclass
class AgentTask:
    """One job for one agent: who does it, with which tools, and how the scripted model plays it."""

    agent: str  # must match an entry in AGENT_PERMISSIONS
    title: str  # e.g. "First email to Maya Thompson at BrightBite Burgers"
    deal_id: str
    system: str
    prompt: str
    tools: list[Tool]
    script: Callable[[Script], None]
    max_turns: int = 15

    def tool(self, name: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.name == name), None)


class ToolCall(Model):
    """One tool call in a run, with the hook's verdict and, for side effects, what the gateway did."""

    tool: str
    input: dict[str, Any]
    verdict: Verdict
    rule: str
    reason: str
    result: str = ""
    is_error: bool = False
    action: Action | None = None
    outcome: Outcome | None = None
    outcome_reason: str = ""
    ms: float = 0


class RunStatus(str, Enum):
    DONE = "done"
    WAITING = "waiting_for_approval"  # a call was deferred: the run stopped there


class AgentRun(Model):
    agent: str
    title: str
    deal_id: str
    model: str
    calls: list[ToolCall] = Field(default_factory=list)
    summary: str = ""
    status: RunStatus = RunStatus.DONE
    cost_usd: float | None = None
    ms: float = 0

    def called(self, tool: str) -> list[ToolCall]:
        return [call for call in self.calls if call.tool == tool]

    def results(self) -> list[tuple[Action, ActionResult]]:
        """Every side effect the gateway handled in this run, in order (executed, queued, blocked or duplicate)."""
        return [(call.action, ActionResult(action_id=call.action.id, outcome=call.outcome, reason=call.outcome_reason))
                for call in self.calls if call.action is not None and call.outcome is not None]


# --- The guard: PreToolUse -> policy ---------------------------------------------------


class ToolGuard:
    """Answers the PreToolUse hook for side-effect tools by asking the autonomy policy.

    AUTO -> allow (the tool body then submits the action, and the gateway checks again before it runs).
    NEEDS_APPROVAL -> the gateway queues the action for a human, and the call is deferred.
    BLOCKED -> the gateway records why, and the call is denied.
    """

    def __init__(self, gateway: ActionGateway, crm: CRM) -> None:
        self._gateway = gateway
        self._crm = crm

    def permission(self, action: Action) -> tuple[Permission, ActionResult | None]:
        deal = self._crm.get_deal(action.deal_id)
        decision = self._gateway.check(action, deal)
        if decision is None:
            return Permission(verdict=Verdict.ALLOW, rule="already_done", reason="This action already ran"), None
        if decision.decision is Decision.AUTO:
            return Permission(verdict=Verdict.ALLOW, rule=decision.rule, reason=decision.reason), None
        result = self._gateway.submit(action, deal)  # queues it for a human, or records why it is blocked
        if result.outcome is Outcome.QUEUED:
            return Permission(verdict=Verdict.DEFER, rule=decision.rule, reason=decision.reason), result
        if result.outcome is Outcome.DUPLICATE:
            return Permission(verdict=Verdict.ALLOW, rule="already_done", reason=result.reason), None
        return Permission(verdict=Verdict.DENY, rule=decision.rule, reason=decision.reason), result

    def submit(self, action: Action) -> ActionResult:
        return self._gateway.submit(action, self._crm.get_deal(action.deal_id))


# --- One run --------------------------------------------------------------------------


class Session:
    """The state of one agent run: the hook's answers, the tool calls, and the trace."""

    def __init__(self, task: AgentTask, guard: ToolGuard, tracer: Tracer, model: str,
                 on_result: OnResult | None = None) -> None:
        self.task = task
        self._guard = guard
        self._tracer = tracer
        self._on_result = on_result
        self.run = AgentRun(agent=task.agent, title=task.title, deal_id=task.deal_id, model=model)
        self._start = time.perf_counter()
        self._allowed: dict[str, list[tuple[Permission, float]]] = {}
        tracer.notify("agent_start", task.agent, {"title": task.title, "deal": task.deal_id})
        tracer.record("run", task.agent, {"title": task.title, "deal": task.deal_id})  # the run's steps follow

    def permission(self, name: str, raw: dict[str, Any]) -> Permission:
        """The PreToolUse hook. Refusals and deferrals are recorded here; allowed calls when they finish."""
        start = time.perf_counter()
        tool = self.task.tool(name)
        if tool is None:
            return self._refuse(name, raw, start, "scoped_tools", f"'{name}' is not one of the {self.task.agent} "
                                                                  f"agent's tools")
        try:
            args = tool.input.model_validate(raw)
        except ValidationError as error:
            return self._refuse(name, raw, start, "invalid_input", _validation_text(error))
        reason = tool.check(args) if tool.check else None
        if reason:
            return self._refuse(name, raw, start, "tool_rule", reason)
        if not tool.effect:
            permission = Permission(verdict=Verdict.ALLOW, rule="read_only", reason="Reads only")
        else:
            action = tool.action(args)
            if action.type is not tool.proposes:  # a tool can only ever propose the one action it declares
                return self._refuse(name, raw, start, "scoped_tools", f"'{name}' may only propose {tool.proposes}")
            permission, result = self._guard.permission(action)
            if permission.verdict is not Verdict.ALLOW:
                text = (f"Waiting for human approval: {permission.reason}. Stop here; the action runs once approved."
                        if permission.verdict is Verdict.DEFER else f"Refused: {permission.reason}")
                self._record(ToolCall(tool=name, input=raw, verdict=permission.verdict, rule=permission.rule,
                                      reason=permission.reason, result=text, is_error=permission.verdict is Verdict.DENY,
                                      action=action, outcome=result.outcome if result else None,
                                      outcome_reason=result.reason if result else ""), start)
                if permission.verdict is Verdict.DEFER:
                    self.run.status = RunStatus.WAITING
                if result is not None and self._on_result:
                    self._on_result(action, result)
                return permission
        self._allowed.setdefault(_key(name, raw), []).append((permission, start))
        self._tracer.notify("tool_start", name, {"agent": self.task.agent, "deal": self.task.deal_id})
        return permission

    def execute(self, name: str, raw: dict[str, Any]) -> ToolResult:
        """The tool body, after the hook allowed it."""
        pending = self._allowed.get(_key(name, raw))
        permission, start = pending.pop(0) if pending else (Permission(verdict=Verdict.ALLOW, rule="read_only",
                                                                       reason="Reads only"), time.perf_counter())
        tool = self.task.tool(name)
        if tool is None:  # the hook refuses these first; this is the second line
            return ToolResult(text=f"Refused: '{name}' is not one of this agent's tools", is_error=True)
        args = tool.input.model_validate(raw)
        if not tool.effect:
            result = tool.run(args)
            self._record(ToolCall(tool=name, input=raw, verdict=Verdict.ALLOW, rule=permission.rule,
                                  reason=permission.reason, result=result.text, is_error=result.is_error), start)
            return result

        action = tool.action(args)
        outcome = self._guard.submit(action)  # the gateway checks the policy again before anything runs
        if self._on_result:
            self._on_result(action, outcome)
        if outcome.outcome in (Outcome.EXECUTED, Outcome.DUPLICATE):
            result = tool.done(args, outcome) if tool.done else ToolResult(text=f"Done: {action.summary}")
            if outcome.outcome is Outcome.DUPLICATE:
                result = ToolResult(text=f"Already done earlier: {action.summary}", data=result.data)
        elif outcome.outcome is Outcome.QUEUED:
            result = ToolResult(text=f"Waiting for human approval: {outcome.reason}")
        else:
            result = ToolResult(text=f"Refused: {outcome.reason}", is_error=True)
        self._record(ToolCall(tool=name, input=raw, verdict=Verdict.ALLOW, rule=permission.rule,
                              reason=permission.reason, result=result.text, is_error=result.is_error, action=action,
                              outcome=outcome.outcome, outcome_reason=outcome.reason), start)
        return result

    def finish(self, summary: str = "", cost_usd: float | None = None) -> AgentRun:
        self.run.summary = summary.strip()
        self.run.cost_usd = cost_usd
        self.run.ms = _ms(self._start)
        self._tracer.record("agent", self.task.agent, {**self._about(), "status": self.run.status.value,
                                                       "summary": self.run.summary, "cost_usd": cost_usd}, self.run.ms)
        return self.run

    def fail(self, error: Exception) -> None:
        """The run broke (e.g. the model call failed): record it, so the trace shows where it stopped."""
        self._tracer.record("agent", self.task.agent, {**self._about(), "status": "failed",
                                                       "error": f"{type(error).__name__}: {error}"}, _ms(self._start))

    def _about(self) -> dict[str, Any]:
        return {"title": self.task.title, "deal": self.task.deal_id, "model": self.run.model,
                "tool_calls": len(self.run.calls), "tools": [tool.name for tool in self.task.tools],
                "system": self.task.system, "prompt": self.task.prompt}

    def _refuse(self, name: str, raw: dict[str, Any], start: float, rule: str, reason: str) -> Permission:
        self._record(ToolCall(tool=name, input=raw, verdict=Verdict.DENY, rule=rule, reason=reason,
                              result=f"Refused: {reason}", is_error=True), start)
        return Permission(verdict=Verdict.DENY, rule=rule, reason=reason)

    def _record(self, call: ToolCall, start: float) -> None:
        call.ms = _ms(start)
        self.run.calls.append(call)
        self._tracer.record("tool", call.tool, {
            "agent": self.task.agent, "deal": self.task.deal_id, "input": _short_input(call.input),
            "verdict": call.verdict.value, "rule": call.rule, "reason": call.reason, "result": call.result[:600],
            "is_error": call.is_error, "action_id": call.action.id if call.action else None,
            "outcome": call.outcome.value if call.outcome else None,
        }, call.ms)


# Called for every side effect the gateway handled during a run, as it happens (the orchestrator moves stages).
OnResult = Callable[[Action, ActionResult], None]


class Runtime(Protocol):
    label: str

    def run(self, task: AgentTask, guard: ToolGuard, tracer: Tracer, on_result: OnResult | None = None) -> AgentRun: ...


class AgentHost:
    """Runs agent tasks with one runtime, one guard and one trace."""

    def __init__(self, runtime: Runtime, guard: ToolGuard, tracer: Tracer) -> None:
        self.runtime = runtime
        self._guard = guard
        self._tracer = tracer

    def run(self, task: AgentTask, on_result: OnResult | None = None) -> AgentRun:
        return self.runtime.run(task, self._guard, self._tracer, on_result)


# --- Scripted runtime ------------------------------------------------------------------


class _Deferred(Exception):
    """The scripted model reached a call that waits for a human: the run stops, as it does with the SDK."""


class Script:
    """What a scripted agent can do: call its tools (through the same hook and gateway) and ask for decisions."""

    def __init__(self, session: Session, answers: LLM, pace: float = 0.0) -> None:
        self._session = session
        self._answers = answers
        self._pace = pace
        self._seen: list[str] = []  # tool results so far: the context a model would have
        self.summary = ""

    def call(self, tool: str, **args: Any) -> ToolResult:
        if self._pace:
            time.sleep(self._pace)  # demo pacing only, so people can watch each step
        permission = self._session.permission(tool, args)
        if permission.verdict is Verdict.DEFER:
            raise _Deferred
        if permission.verdict is Verdict.DENY:
            result = ToolResult(text=f"Refused: {permission.reason}", is_error=True)
        else:
            result = self._session.execute(tool, args)
        self._seen.append(f"[{tool}] {result.text}")
        return result

    def ask(self, schema: type[T], note: str = "") -> T:
        """A decision, read from the scripted answers with everything the agent has seen so far."""
        task = self._session.task
        context = "\n\n".join(self._seen)
        return self._answers.complete(system=task.system, prompt=f"{task.prompt}\n\n{context}{note}", schema=schema)

    def say(self, text: str) -> None:
        self.summary = text


class ScriptedRuntime:
    """Plays each agent's scripted decisions through the real tools, hook and gateway. No model, no API key."""

    label = "scripted answers"

    def __init__(self, answers: LLM, pace: float = 0.0) -> None:
        self.answers = answers
        self.pace = pace  # seconds before each tool call: only the live UI sets it, so the run can be watched

    def run(self, task: AgentTask, guard: ToolGuard, tracer: Tracer, on_result: OnResult | None = None) -> AgentRun:
        session = Session(task, guard, tracer, model=self.label, on_result=on_result)
        script = Script(session, self.answers, self.pace)
        try:
            task.script(script)
        except _Deferred:
            pass
        except Exception as error:
            session.fail(error)
            raise
        return session.finish(script.summary)


# --- Claude runtime --------------------------------------------------------------------


QueryFn = Callable[..., AsyncIterator[Any]]
ServerFn = Callable[..., Any]


class ClaudeRuntime:
    """The real model: Claude picks the tools, the hook asks the policy, the gateway runs the side effects."""

    def __init__(self, model: str = DEFAULT_MODEL, *, max_budget_usd: float = 0.50,
                 query_fn: QueryFn | None = None, server_fn: ServerFn | None = None) -> None:
        self.model = model
        self.max_budget_usd = max_budget_usd
        self._query = query_fn
        self._server = server_fn

    @property
    def label(self) -> str:
        return self.model

    def run(self, task: AgentTask, guard: ToolGuard, tracer: Tracer, on_result: OnResult | None = None) -> AgentRun:
        # Without an explicit key, the SDK may fall back to whatever account Claude Code is logged into on this
        # machine. Refuse instead of guessing.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY is not set. Put it in .env (see README).")
        session = Session(task, guard, tracer, model=self.model, on_result=on_result)
        try:
            return asyncio.run(self._run(session))
        except Exception as error:
            session.fail(error)
            raise

    def options(self, session: Session) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, create_sdk_mcp_server, tool

        task = session.task

        def sdk_tool(definition: Tool) -> Any:
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                result = session.execute(definition.name, args)
                return {"content": [{"type": "text", "text": result.text}], "is_error": result.is_error}

            return tool(definition.name, definition.description, _schema(definition.input))(handler)

        async def pre_tool_use(hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            name = hook_input.get("tool_name", "")
            if name.startswith(TOOL_PREFIX):
                permission = session.permission(name.removeprefix(TOOL_PREFIX), hook_input.get("tool_input") or {})
            else:  # never offered (no built-in tools), refused if asked anyway
                permission = Permission(verdict=Verdict.DENY, rule="scoped_tools", reason=f"'{name}' is not allowed")
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": permission.verdict.value,
                                           "permissionDecisionReason": permission.reason}}

        server = (self._server or create_sdk_mcp_server)(name=SERVER, version="1.0.0",
                                                         tools=[sdk_tool(item) for item in task.tools])
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=task.system,
            tools=[],  # no built-in tools at all: no files, shell or web
            allowed_tools=[TOOL_PREFIX + item.name for item in task.tools],
            mcp_servers={SERVER: server},
            strict_mcp_config=True,  # only this server: no MCP servers from the developer's own config
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            permission_mode="dontAsk",  # anything not on this agent's list is refused, never asked
            setting_sources=[],  # ignore the developer's personal Claude Code settings
            max_turns=task.max_turns,
            max_budget_usd=self.max_budget_usd,
        )

    async def _run(self, session: Session) -> AgentRun:
        from claude_agent_sdk import AssistantMessage, ClaudeSDKError, ResultMessage, TextBlock

        query = self._query or _sdk_query()
        result, last_text = None, ""
        try:
            async for message in query(prompt=session.task.prompt, options=self.options(session)):
                if isinstance(message, AssistantMessage):
                    text = " ".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()
                    last_text = text or last_text
                elif isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as error:
            raise LLMError(f"The model call failed: {error}") from error

        if result is None:
            raise LLMError("The model returned no result")
        if result.deferred_tool_use is not None:
            session.run.status = RunStatus.WAITING
        elif result.is_error:
            raise LLMError(f"The {session.task.agent} agent stopped ({result.subtype}): {result.result or result.errors}")
        return session.finish(result.result or last_text, result.total_cost_usd)


def _sdk_query() -> QueryFn:
    from claude_agent_sdk import query

    return query


# --- Helpers -------------------------------------------------------------------------------


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def _key(name: str, raw: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(raw, sort_keys=True, default=str)}"


def _validation_text(error: ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(str(part) for part in item['loc']) or 'input'}: {item['msg']}"
                         for item in error.errors())
    return f"Invalid input ({problems}). Fix it and call the tool again."


def _short_input(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: (value if not isinstance(value, str) or len(value) <= 300 else value[:300] + "…")
            for key, value in raw.items()}


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
