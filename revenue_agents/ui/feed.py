"""The agents' work as a feed, read from the trace and the world's log, so it shows what really ran.

Each entry is something that arrived (a lead, a reply, a call, your decision,
the scheduler) with the agent runs it started, and each run with the tools the
agent chose, in order, and what the hook and the gateway did with each call.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from revenue_agents.team import LABELS
from revenue_agents.tracing import Span
from revenue_agents.world import World


@dataclass
class ToolStep:
    tool: str
    verdict: str
    outcome: str | None
    rule: str
    reason: str
    result: str
    is_error: bool = False

    @property
    def tone(self) -> str:
        """read, done, fix (code sent it back to the agent), asked (waits for you) or refused."""
        if self.verdict == "defer":
            return "asked"
        if self.verdict == "deny":
            return "refused"
        if self.is_error:
            return "fix"
        if self.outcome in ("executed", "duplicate"):
            return "done"
        if self.outcome == "blocked":
            return "refused"
        return "read"


@dataclass
class Run:
    agent: str
    title: str
    deal: str
    at: dt.datetime
    status: str
    summary: str
    model: str
    cost: float | None
    ms: float
    tools: list[ToolStep] = field(default_factory=list)
    error: str | None = None

    @property
    def label(self) -> str:
        return LABELS.get(self.agent, self.agent)

    @property
    def outcome(self) -> str:
        tones = {step.tone for step in self.tools}
        if self.error:
            return "failed"
        if "asked" in tones:
            return "asked you"
        if "done" in tones:
            return "on its own"
        if "refused" in tones:
            return "refused"
        return "read only"


@dataclass
class Entry:
    at: dt.datetime
    label: str
    source: str
    status: str
    steps: list[str]
    runs: list[Run]


def runs_in(spans: list[Span]) -> list[Run]:
    """Agent runs, in order, each with the tool calls it made (recorded before the run's own span)."""
    pending: dict[str, list[ToolStep]] = defaultdict(list)
    runs = []
    for span in spans:
        if span.kind == "tool":
            data = span.data
            pending[data.get("agent", "")].append(ToolStep(span.name, data.get("verdict", ""), data.get("outcome"),
                                                           data.get("rule", ""), data.get("reason", ""),
                                                           data.get("result", ""), data.get("is_error", False)))
        elif span.kind == "agent" and "tool_calls" in span.data:
            data = span.data
            runs.append(Run(span.name, data.get("title", ""), data.get("deal", ""), span.at, data.get("status", ""),
                            data.get("summary", ""), data.get("model", ""), data.get("cost_usd"),
                            span.duration_ms or 0, pending.pop(span.name, []), data.get("error")))
    return runs


def entries(world: World, spans: list[Span], limit: int = 25) -> list[Entry]:
    """The newest arrivals first, each with the agent runs it started."""
    by_event: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        by_event[span.event_id].append(span)
    result = []
    for delivered in reversed(world.log()[-limit:]):
        # A held event runs later, when you release the agents: its runs show under the release, not here.
        runs = [] if delivered.status == "held" else [
            run for event_id in delivered.events for run in runs_in(by_event.get(event_id, []))]
        result.append(Entry(delivered.at, delivered.label, delivered.source, delivered.status, delivered.steps, runs))
    return result


def acted_on_their_own(spans: list[Span]) -> int:
    """Side effects the agents ran without a human (reads and checks don't count)."""
    return sum(1 for span in spans if span.kind == "tool" and span.data.get("verdict") == "allow"
               and span.data.get("outcome") == "executed")


def trail_for(spans: list[Span], action_id: str) -> Run | None:
    """The agent run that proposed this action: what it read and tried before it stopped for you."""
    hits = [span for span in spans if span.kind == "tool" and span.data.get("action_id") == action_id]
    if not hits:
        return None
    event_id, agent = hits[0].event_id, hits[0].data.get("agent")
    runs = [run for run in runs_in([span for span in spans if span.event_id == event_id]) if run.agent == agent]
    return runs[-1] if runs else None
