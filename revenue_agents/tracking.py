"""Tracking: stall detection, reminders and the daily digest.

Run on a schedule (events `sla.check` and `digest.daily`). The stall limits
come from each segment pack, so "stalled" can mean 3 days for one segment
and 5 for another without code changes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import Field

from revenue_agents.crm import CRM
from revenue_agents.guardrails.approvals import ApprovalQueue, ApprovalRequest, ApprovalStatus
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.held_events import HeldEvents
from revenue_agents.lessons import LessonStore
from revenue_agents.models import Deal, Model, Stage
from revenue_agents.segments import SegmentRegistry
from revenue_agents.stages import ORDER
from revenue_agents.storage import write_json

CLOSED = (Stage.HANDOFF, Stage.CLOSED_LOST)

# Flags a human should look at. Informational flags (e.g. meeting_requested) are left out.
ATTENTION_FLAGS = ("pricing_requested", "possible_prompt_injection", "objection", "unsubscribe", "io_blocked",
                   "outreach_draft_rejected", "unknown_product_mentioned", "check_flight_dates")


class StalledDeal(Model):
    deal_id: str
    company: str
    contact: str
    stage: Stage
    days_in_stage: float
    limit_days: int


class WaitingApproval(Model):
    request_id: str
    summary: str
    hours_waiting: float
    overdue: bool


class Digest(Model):
    date: dt.date
    pipeline: dict[str, int]
    approvals_waiting: list[WaitingApproval]
    stalled: list[StalledDeal]
    needs_attention: dict[str, list[str]]
    new_lessons: list[str]
    stuck_actions: list[str] = Field(default_factory=list)
    approved_not_executed: list[str] = Field(default_factory=list)
    held_events: list[str] = Field(default_factory=list)

    def render(self) -> str:
        lines = [f"Daily digest {self.date.isoformat()}",
                 "Pipeline: " + (", ".join(f"{stage} {count}" for stage, count in self.pipeline.items()) or "empty")]
        lines.append(f"Approvals waiting ({len(self.approvals_waiting)}):")
        lines += [f"  - {a.summary} ({a.hours_waiting:.0f}h{', OVERDUE' if a.overdue else ''})"
                  for a in self.approvals_waiting] or ["  none"]
        lines.append(f"Stalled deals ({len(self.stalled)}):")
        lines += [f"  - {s.deal_id} {s.company}: {s.stage.value} for {s.days_in_stage:.0f} days "
                  f"(limit {s.limit_days}); follow up with {s.contact}" for s in self.stalled] or ["  none"]
        lines.append(f"Needs attention ({len(self.needs_attention)}):")
        lines += [f"  - {deal_id}: {', '.join(flags)}" for deal_id, flags in self.needs_attention.items()] or ["  none"]
        lines.append(f"New lessons ({len(self.new_lessons)}):")
        lines += [f"  - {text}" for text in self.new_lessons] or ["  none"]
        lines.append(f"Approved but not executed ({len(self.approved_not_executed)}):")
        lines += [f"  - {summary} (use Run now)" for summary in self.approved_not_executed] or ["  none"]
        lines.append(f"Held by the kill switch ({len(self.held_events)}):")
        lines += [f"  - {event}" for event in self.held_events] or ["  none"]
        lines.append(f"Stuck actions ({len(self.stuck_actions)}):")
        lines += [f"  - {action_id} (started but never finished: check by hand)" for action_id in self.stuck_actions] or [
            "  none"]
        return "\n".join(lines)


class Tracker:
    def __init__(self, crm: CRM, approvals: ApprovalQueue, lessons: LessonStore, segments: SegmentRegistry,
                 claims: ActionClaims, digest_dir: Path, held: HeldEvents) -> None:
        self._crm = crm
        self._approvals = approvals
        self._lessons = lessons
        self._segments = segments
        self._claims = claims
        self._digest_dir = Path(digest_dir)
        self._held = held

    def approved_waiting(self) -> list[ApprovalRequest]:
        """Approved by a human but never executed (no claim): blocked by the kill switch, or failed."""
        return [request for request in self._approvals.all()
                if request.status is ApprovalStatus.APPROVED and not self._claims.exists(request.action.id)]

    def stalled(self, now: dt.datetime) -> list[StalledDeal]:
        result = []
        for deal in self._crm.deals():
            if deal.stage in CLOSED or deal.stage_entered_at is None or self._nurtured(deal):
                continue
            limit = self._segments.get(deal.segment).tracking.limit_for(deal.stage)
            days = (now - deal.stage_entered_at).total_seconds() / 86400
            if limit and days > limit:
                lead = self._crm.get_lead(deal.lead_id)
                result.append(StalledDeal(deal_id=deal.id, company=lead.company, contact=lead.contact.name,
                                          stage=deal.stage, days_in_stage=days, limit_days=limit))
        return sorted(result, key=lambda stalled: stalled.deal_id)

    def _nurtured(self, deal: Deal) -> bool:
        """Scored leads below the segment's outreach tiers are parked on purpose: not stalled."""
        outreach_tiers = self._segments.get(deal.segment).messaging.outreach_tiers
        return deal.stage is Stage.SCORED and deal.score is not None and deal.score.tier not in outreach_tiers

    def digest(self, now: dt.datetime) -> Digest:
        deals = self._crm.deals()
        pipeline = {stage.value: count for stage in [*ORDER, Stage.CLOSED_LOST]
                    if (count := sum(deal.stage is stage for deal in deals))}
        waiting = []
        for request in self._approvals.pending():
            hours = (now - request.created_at).total_seconds() / 3600
            limit = self._segments.get(self._crm.get_deal(request.action.deal_id).segment).tracking
            waiting.append(WaitingApproval(request_id=request.id, summary=request.action.summary, hours_waiting=hours,
                                           overdue=hours > limit.approval_reminder_hours))
        attention = {deal.id: flags for deal in sorted(deals, key=lambda d: d.id)
                     if (flags := [f for f in deal.flags if f in ATTENTION_FLAGS or f.startswith("stalled_")])}
        new_lessons = [lesson.text for lesson in self._lessons.all() if lesson.created_at.date() == now.date()]

        digest = Digest(date=now.date(), pipeline=pipeline, approvals_waiting=waiting, stalled=self.stalled(now),
                        needs_attention=attention, new_lessons=new_lessons, stuck_actions=self._claims.in_progress(),
                        approved_not_executed=[r.action.summary for r in self.approved_waiting()],
                        held_events=[f"{e.type.value} ({e.id})" for e in self._held.all()])
        write_json(self._digest_dir / f"{digest.date.isoformat()}.json", digest.model_dump(mode="json"))
        return digest
