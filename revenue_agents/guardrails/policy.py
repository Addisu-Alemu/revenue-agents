"""Autonomy policy: decides whether an action runs, waits for a human, or is blocked.

This is plain Python, not a prompt. Agents can only propose actions; they
cannot talk their way past these rules. Inputs that matter for safety (tier,
stage) are read from the orchestrator's deal state, never from the action
itself, so neither an agent nor an injected email can claim a lower risk.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import Field

from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.models import Deal, Model, Stage
from revenue_agents.segments import SegmentRegistry


class ActionType(str, Enum):
    ENRICH_LEAD = "enrich_lead"
    SCORE_LEAD = "score_lead"
    UPDATE_DEAL = "update_deal"
    FLAG_DEAL = "flag_deal"
    CLASSIFY_REPLY = "classify_reply"
    RECORD_LESSON = "record_lesson"
    SEND_EMAIL = "send_email"
    PREPARE_CALL_BRIEF = "prepare_call_brief"
    DRAFT_IO = "draft_io"
    SEND_PRICING = "send_pricing"
    SEND_IO = "send_io"
    SEND_FOR_SIGNATURE = "send_for_signature"
    CLOSE_DEAL = "close_deal"


class Decision(str, Enum):
    AUTO = "auto"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"


class Action(Model):
    """Something an agent wants to do. The id is an idempotency key: same id, same action."""

    id: str
    type: ActionType
    agent: str
    deal_id: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(Model):
    decision: Decision
    rule: str
    reason: str


# Each agent may only propose the action types it needs (scoped tools).
AGENT_PERMISSIONS: dict[str, frozenset[ActionType]] = {
    "research": frozenset({ActionType.ENRICH_LEAD}),
    "scoring": frozenset({ActionType.SCORE_LEAD}),
    "outreach": frozenset({ActionType.SEND_EMAIL}),
    "reply_triage": frozenset({ActionType.CLASSIFY_REPLY, ActionType.FLAG_DEAL, ActionType.RECORD_LESSON}),
    "call_intel": frozenset({ActionType.UPDATE_DEAL, ActionType.RECORD_LESSON}),
    "call_brief": frozenset({ActionType.PREPARE_CALL_BRIEF}),
    "deal_desk": frozenset(
        {ActionType.DRAFT_IO, ActionType.SEND_PRICING, ActionType.SEND_IO, ActionType.SEND_FOR_SIGNATURE}
    ),
    "orchestrator": frozenset(
        {ActionType.UPDATE_DEAL, ActionType.FLAG_DEAL, ActionType.CLOSE_DEAL, ActionType.RECORD_LESSON}
    ),
}

# High-risk actions: always need a human, whatever the tier or segment.
ALWAYS_NEEDS_APPROVAL = frozenset({ActionType.SEND_PRICING, ActionType.SEND_IO, ActionType.SEND_FOR_SIGNATURE})

# Stages before any outreach was sent: an email from here is a first touch.
PRE_OUTREACH_STAGES = frozenset({Stage.PROSPECT, Stage.ENRICHED, Stage.SCORED})

# Catches pricing in outbound text even when no agent labelled it as pricing.
PRICING_PATTERN = re.compile(
    r"\$\s?\d|\b\d[\d,.]*\s?(?:usd|dollars|k)\b|\brate card\b|\bpric(?:e|es|ing)\b|\bdiscount|\bcpm\b",
    re.IGNORECASE,
)


class AutonomyPolicy:
    def __init__(self, segments: SegmentRegistry, kill_switch: KillSwitch) -> None:
        self._segments = segments
        self._kill_switch = kill_switch

    def decide(self, action: Action, deal: Deal) -> PolicyDecision:
        """Apply the rules in order. The first rule that matches wins."""
        if self._kill_switch.is_engaged():
            return _blocked("kill_switch", f"Kill switch engaged: {self._kill_switch.reason()}")

        allowed = AGENT_PERMISSIONS.get(action.agent, frozenset())
        if action.type not in allowed:
            return _blocked("agent_scope", f"Agent '{action.agent}' is not allowed to {action.type.value}")

        if action.type in ALWAYS_NEEDS_APPROVAL:
            return _approval("high_risk_action", f"{action.type.value} always needs human approval")

        if action.type is ActionType.SEND_EMAIL:
            return self._decide_email(action, deal)

        return PolicyDecision(decision=Decision.AUTO, rule="low_risk", reason=f"{action.type.value} is low risk")

    def _decide_email(self, action: Action, deal: Deal) -> PolicyDecision:
        text = f"{action.payload.get('subject', '')}\n{action.payload.get('body', '')}"
        if PRICING_PATTERN.search(text):
            return _approval("pricing_in_email", "Email mentions pricing, which needs human approval")

        if deal.score is None:
            return _approval("unscored_lead", "Lead has no score yet, so its risk is unknown")

        first_touch = deal.stage in PRE_OUTREACH_STAGES
        approval_tiers = self._segments.get(deal.segment).autonomy.first_touch_approval_tiers
        if first_touch and deal.score.tier in approval_tiers:
            return _approval("first_touch_tier", f"First contact with a Tier {deal.score.tier.value} lead")
        if deal.stage is Stage.REPLIED and deal.score.tier in approval_tiers:
            # The agent is answering a top lead's own words (e.g. a product question): a human checks it.
            return _approval("reply_to_top_tier", f"Answer to a Tier {deal.score.tier.value} lead's reply")

        return PolicyDecision(decision=Decision.AUTO, rule="low_risk_email", reason="Email to a lower-risk lead")


def _approval(rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision(decision=Decision.NEEDS_APPROVAL, rule=rule, reason=reason)


def _blocked(rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision(decision=Decision.BLOCKED, rule=rule, reason=reason)
