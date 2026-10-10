"""The single place where agents talk to an LLM.

Agents depend on the LLM interface, not on the SDK:
- ClaudeLLM: the real model, via the Claude Agent SDK, locked down to "read
  text, return validated JSON". No built-in tools (no files, shell or web),
  no user settings, a turn limit and a spend cap per call. Any real-world
  effect still has to go through the ActionGateway and its policy.
- FakeLLM: scripted answers for tests. Free, offline and deterministic.

The model is a per-instance setting, so each agent can use the model that
fits its job without code changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    """The model call failed or returned something that doesn't fit the schema."""


class LLM(Protocol):
    def complete(self, *, system: str, prompt: str, schema: type[T]) -> T:
        """Ask the model and return its answer, validated against `schema`."""
        ...


# --- Real model ----------------------------------------------------------------


QueryFn = Callable[..., AsyncIterator[Any]]


def _sdk_query() -> QueryFn:
    from claude_agent_sdk import query

    return query


class ClaudeLLM:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        max_turns: int = 3,
        max_budget_usd: float = 0.50,
        query_fn: QueryFn | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.last_cost_usd: float | None = None
        self._query = query_fn

    def complete(self, *, system: str, prompt: str, schema: type[T]) -> T:
        # Without an explicit key, the SDK may fall back to whatever account
        # Claude Code is logged into on this machine. Refuse instead of guessing.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY is not set. Put it in .env (see README).")
        return asyncio.run(self._complete(system, prompt, schema))

    def options(self, system: str, schema: type[BaseModel]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=system,
            tools=[],  # no built-in tools at all: no files, shell or web
            allowed_tools=[],
            setting_sources=[],  # ignore the developer's personal Claude Code settings
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            output_format={"type": "json_schema", "schema": schema.model_json_schema()},
        )

    async def _complete(self, system: str, prompt: str, schema: type[T]) -> T:
        from claude_agent_sdk import ClaudeSDKError, ResultMessage

        query = self._query or _sdk_query()
        result = None
        try:
            async for message in query(prompt=prompt, options=self.options(system, schema)):
                if isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as error:
            # e.g. no credit, bad key, network or CLI problems: one clear message, no traceback
            raise LLMError(f"The model call failed: {error}") from error

        if result is None:
            raise LLMError("The model returned no result")
        self.last_cost_usd = result.total_cost_usd
        if result.is_error:
            raise LLMError(f"The model call failed ({result.subtype}): {result.result or result.errors}")

        data = result.structured_output if result.structured_output is not None else _json_from_text(result.result)
        return _validate(schema, data)


# --- Test double -----------------------------------------------------------------


@dataclass
class LLMCall:
    system: str
    prompt: str
    schema: str


Responder = dict[str, Any] | Callable[[LLMCall], dict[str, Any]]


@dataclass
class FakeLLM:
    """Answers by schema name, e.g. {"ReplyClassification": {...}} or a function of the call."""

    responses: dict[str, Responder]
    calls: list[LLMCall] = field(default_factory=list)

    def complete(self, *, system: str, prompt: str, schema: type[T]) -> T:
        call = LLMCall(system=system, prompt=prompt, schema=schema.__name__)
        self.calls.append(call)
        if schema.__name__ not in self.responses:
            raise LLMError(f"FakeLLM has no scripted answer for {schema.__name__}")
        responder = self.responses[schema.__name__]
        return _validate(schema, responder(call) if callable(responder) else responder)


# --- Helpers ---------------------------------------------------------------------


def _validate(schema: type[T], data: Any) -> T:
    try:
        return schema.model_validate(data)
    except ValidationError as error:
        raise LLMError(f"Model answer doesn't match {schema.__name__}:\n{error}") from error


def _json_from_text(text: str | None) -> Any:
    """Fallback when no structured output came back: parse JSON from the text, fenced or not."""
    if not text:
        raise LLMError("The model returned no answer")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    try:
        return json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError as error:
        raise LLMError(f"The model answer is not valid JSON: {text[:200]}") from error
