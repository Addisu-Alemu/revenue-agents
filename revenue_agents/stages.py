"""Deal stages and their entry (exit) criteria.

A deal moves forward one stage at a time, and only when the evidence for the
new stage exists: a score before SCORED, a sent email before OUTREACH, the
proposal fields before QUALIFIED, and so on. The orchestrator checks these
before every transition, so a deal can't jump ahead on an agent's say-so.
"""

from __future__ import annotations

from collections.abc import Callable

from revenue_agents.crm import Activity
from revenue_agents.models import Deal, Stage

ORDER = [
    Stage.PROSPECT, Stage.ENRICHED, Stage.SCORED, Stage.OUTREACH, Stage.REPLIED, Stage.MEETING,
    Stage.QUALIFIED, Stage.PROPOSAL, Stage.SIGNATURE, Stage.HANDOFF,
]

# Fields a proposal / IO can't be drafted without.
PROPOSAL_FIELDS = ("budget_usd", "products", "flight_start", "flight_end", "decision_maker")


def _has_activity(kind: str, label: str) -> Callable[[Deal, list[Activity]], list[str]]:
    def check(deal: Deal, timeline: list[Activity]) -> list[str]:
        return [] if any(activity.kind == kind for activity in timeline) else [label]

    return check


def _reply_to_us(deal: Deal, timeline: list[Activity]) -> list[str]:
    # A reply only counts once we emailed them: a message from a lead we never contacted isn't a reply.
    return _has_activity("email_sent", "a sent email")(deal, timeline) + _has_activity("reply", "a classified reply")(
        deal, timeline)


def _proposal_fields(deal: Deal, timeline: list[Activity]) -> list[str]:
    return [name for name in PROPOSAL_FIELDS if not getattr(deal.fields, name)]


CRITERIA: dict[Stage, Callable[[Deal, list[Activity]], list[str]]] = {
    Stage.PROSPECT: lambda deal, timeline: [],
    Stage.ENRICHED: lambda deal, timeline: [] if deal.enrichment else ["enrichment"],
    Stage.SCORED: lambda deal, timeline: [] if deal.score else ["score"],
    Stage.OUTREACH: _has_activity("email_sent", "a sent email"),
    Stage.REPLIED: _reply_to_us,
    Stage.MEETING: lambda deal, timeline: [] if "meeting_requested" in deal.flags else ["a meeting request"],
    Stage.QUALIFIED: _proposal_fields,
    Stage.PROPOSAL: _has_activity("document", "an IO draft"),
    Stage.SIGNATURE: _has_activity("io_sent", "an IO sent to the customer"),
    Stage.HANDOFF: _has_activity("signed", "a signed IO"),
    Stage.CLOSED_LOST: lambda deal, timeline: [],
}


def missing_for(stage: Stage, deal: Deal, timeline: list[Activity]) -> list[str]:
    """What the deal still lacks before it may enter `stage` (empty list = ready)."""
    return CRITERIA[stage](deal, timeline)


def is_forward(current: Stage, target: Stage) -> bool:
    """Deals only move forward. Closing as lost is allowed from any open stage."""
    if current is Stage.CLOSED_LOST:
        return False
    if target is Stage.CLOSED_LOST:
        return True
    return ORDER.index(target) > ORDER.index(current)
