"""What every agent shares: a name, and a way to propose actions with stable ids.

Agents never execute. The AI agents (research, outreach, reply, call intel)
build a task for the runtime: their tools propose actions, and each one goes
through the PreToolUse hook and the gateway. The code skills (call brief, deal
desk) return an AgentResult: their findings plus the actions they propose,
which the orchestrator sends to the gateway. Either way the autonomy policy
decides what runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from revenue_agents.guardrails.policy import Action, ActionType
from revenue_agents.models import Deal

T = TypeVar("T")


@dataclass
class AgentResult(Generic[T]):
    output: T
    actions: list[Action] = field(default_factory=list)


class Agent:
    """Base class. `name` must match an entry in AGENT_PERMISSIONS."""

    name: str = ""

    def propose(self, type_: ActionType, deal: Deal, key: str, summary: str, payload: dict[str, Any]) -> Action:
        # A deterministic id (same deal, same step -> same id) makes retries idempotent.
        return Action(
            id=f"{type_.value}:{deal.id}:{key}",
            type=type_,
            agent=self.name,
            deal_id=deal.id,
            summary=summary,
            payload=payload,
        )
