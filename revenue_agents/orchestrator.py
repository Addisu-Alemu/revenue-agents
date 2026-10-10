"""Orchestrator: owns deal state and decides which agent works next.

It reacts to events, runs the right agents, sends every proposed action
through the gateway, and moves the deal forward only when the next stage's
criteria are met. Each event id is claimed before processing, so a webhook
delivered twice is handled once; if processing fails, the claim is released
so a retry can run.

    lead.created     -> research -> scoring -> outreach (Tier A waits for approval)
    approval.decided -> execute or drop the approved/rejected action
    reply.received   -> reply triage -> flags (meeting / pricing / injection / unsubscribe)
    call.completed   -> call intel -> qualified? -> deal desk drafts IO (sending waits for approval)
    sla.check        -> flag stalled deals and remind the rep (once per deal and stage)
    digest.daily     -> summary: approvals waiting, stalled and flagged deals, new lessons, stuck actions
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from revenue_agents.agents.call_intel import CallIntelAgent
from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.agents.outreach import DraftRejectedError, OutreachAgent
from revenue_agents.agents.reply_triage import ReplyTriageAgent
from revenue_agents.agents.research import ResearchAgent
from revenue_agents.agents.scoring import ScoringAgent
from revenue_agents.crm import CRM
from revenue_agents.events import Event, EventType
from revenue_agents.guardrails.approvals import ApprovalQueue
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.gateway import ActionGateway, ActionResult, Outcome
from revenue_agents.guardrails.policy import Action, ActionType
from revenue_agents.lessons import LessonStore
from revenue_agents.models import Deal, Model, Reply, Stage
from revenue_agents.segments import SegmentRegistry
from revenue_agents.stages import ORDER, is_forward, missing_for
from revenue_agents.storage import utc_now
from revenue_agents.tracking import Tracker

# Executing these actions is the evidence for the next stage.
STAGE_AFTER = {ActionType.SEND_EMAIL: Stage.OUTREACH, ActionType.DRAFT_IO: Stage.PROPOSAL,
               ActionType.SEND_IO: Stage.SIGNATURE}


@dataclass
class Agents:
    research: ResearchAgent
    scoring: ScoringAgent
    outreach: OutreachAgent
    reply_triage: ReplyTriageAgent
    call_intel: CallIntelAgent
    deal_desk: DealDeskAgent


class EventReport(Model):
    """What happened while handling one event, in plain words (for the demo and the UI)."""

    event_id: str
    status: str  # "processed" or "duplicate"
    steps: list[str] = Field(default_factory=list)


class Orchestrator:
    name = "orchestrator"

    def __init__(self, crm: CRM, lessons: LessonStore, gateway: ActionGateway, approvals: ApprovalQueue,
                 audit: AuditLog, segments: SegmentRegistry, agents: Agents, events: ActionClaims,
                 tracker: Tracker) -> None:
        self._crm = crm
        self._lessons = lessons
        self._gateway = gateway
        self._approvals = approvals
        self._audit = audit
        self._segments = segments
        self._agents = agents
        self._events = events
        self._tracker = tracker
        self._handlers = {
            EventType.LEAD_CREATED: self._on_lead_created,
            EventType.APPROVAL_DECIDED: self._on_approval,
            EventType.REPLY_RECEIVED: self._on_reply,
            EventType.CALL_COMPLETED: self._on_call,
            EventType.SLA_CHECK: self._on_sla_check,
            EventType.DIGEST_DAILY: self._on_digest,
        }

    def handle(self, event: Event) -> EventReport:
        if not self._events.claim(event.id):
            self._audit.record(self.name, "event_duplicate", action_id=event.id, detail=event.type.value)
            return EventReport(event_id=event.id, status="duplicate", steps=["Already handled: ignored"])

        self._audit.record(self.name, "event_received", action_id=event.id, detail=event.type.value)
        try:
            steps = self._handlers[event.type](event)
        except Exception as error:
            self._events.release(event.id)  # let a retry process it
            self._audit.record(self.name, "event_failed", action_id=event.id, detail=f"{type(error).__name__}: {error}")
            raise
        self._events.complete(event.id)
        return EventReport(event_id=event.id, status="processed", steps=steps)

    # --- Event handlers --------------------------------------------------------

    def _on_lead_created(self, event: Event) -> list[str]:
        lead = self._crm.get_lead(event.payload["lead_id"])
        deal = self._crm.deal_for_lead(lead.id)
        steps = [f"New lead: {lead.company} ({lead.segment}) -> deal {deal.id}"]

        steps += self._submit(self._agents.research.run(lead, deal).actions)
        steps += self._advance(deal.id, Stage.ENRICHED)
        deal = self._crm.get_deal(deal.id)
        if deal.enrichment is None:
            return steps + ["Stopped: lead could not be enriched"]

        steps += self._submit(self._agents.scoring.run(lead, deal, deal.enrichment).actions)
        steps += self._advance(deal.id, Stage.SCORED)
        deal = self._crm.get_deal(deal.id)
        if deal.score is None:
            return steps + ["Stopped: lead could not be scored"]

        if deal.score.tier not in self._segments.get(lead.segment).messaging.outreach_tiers:
            return steps + [f"Tier {deal.score.tier.value}: no outreach, left for nurture"]

        lessons = self._lessons.for_segment(lead.segment, purpose="outreach")
        try:
            result = self._agents.outreach.run(lead, deal, deal.enrichment, lessons)
        except DraftRejectedError as error:
            return steps + self._flag(deal, "outreach_draft_rejected", str(error))
        if result.output.lessons_used:
            steps.append(f"Draft uses lessons: {', '.join(result.output.lessons_used)}")
        return steps + self._submit(result.actions)

    def _on_approval(self, event: Event) -> list[str]:
        request_id, reviewer = event.payload["request_id"], event.payload["reviewer"]
        if not event.payload["approved"]:
            self._approvals.reject(request_id, reviewer, event.payload.get("note"))
            return [f"Rejected by {reviewer}: {request_id}"]

        request = self._approvals.approve(request_id, reviewer, event.payload.get("note"))
        deal = self._crm.get_deal(request.action.deal_id)
        result = self._gateway.execute_approved(request_id, deal)
        return [f"Approved by {reviewer}: {request.action.summary} -> {result.outcome.value}"] + self._after(
            request.action, result)

    def _on_reply(self, event: Event) -> list[str]:
        reply = Reply.model_validate(event.payload["reply"])
        lead = self._crm.get_lead(reply.lead_id)
        deal = self._crm.deal_for_lead(lead.id)
        result = self._agents.reply_triage.run(reply, lead, deal)
        triage = result.output
        steps = [f"Reply {reply.id} from {lead.contact.name}: {triage.classification.intent}"]
        if triage.flags:
            steps.append(f"Flags: {', '.join(triage.flags)}")
        steps += self._submit(result.actions)
        steps += self._advance(deal.id, Stage.REPLIED)

        flags = set(self._crm.get_deal(deal.id).flags)
        if "unsubscribe" in triage.flags:
            return steps + self._advance(deal.id, Stage.CLOSED_LOST, reason="contact unsubscribed")
        if "meeting_requested" in flags:
            steps += self._advance(deal.id, Stage.MEETING)
        if "pricing_requested" in triage.flags:
            steps.append("Pricing question: a human must answer (pricing always needs approval)")
        if "possible_prompt_injection" in triage.flags:
            steps.append("Suspicious content: ignored as instructions, no lesson learned")
        return steps

    def _on_call(self, event: Event) -> list[str]:
        lead = self._crm.get_lead(event.payload["lead_id"])
        deal = self._crm.deal_for_lead(lead.id)
        result = self._agents.call_intel.run(event.payload["transcript_id"], event.payload["transcript"], lead, deal)
        intel = result.output
        steps = [f"Call {intel.transcript_id}: budget {intel.fields.budget_usd}, signer {intel.fields.decision_maker}"]
        steps += [f"Lesson learned: {lesson.text}" for lesson in intel.lessons]
        steps += self._submit(result.actions)

        steps += self._advance(deal.id, Stage.QUALIFIED)
        deal = self._crm.get_deal(deal.id)
        if deal.stage is Stage.CLOSED_LOST or ORDER.index(deal.stage) < ORDER.index(Stage.QUALIFIED):
            return steps + ["Not qualified yet: no IO drafted"]
        if deal.stage is not Stage.QUALIFIED:
            return steps + [f"Deal already at {deal.stage.value}: IO not redrafted"]

        io = self._agents.deal_desk.run(lead, deal)
        if io.output.errors:
            return steps + self._flag(deal, "io_blocked", "; ".join(io.output.errors))
        return steps + self._submit(io.actions)

    def _on_sla_check(self, event: Event) -> list[str]:
        stalled = self._tracker.stalled(event.now)
        steps = [f"SLA check: {len(stalled)} stalled deal(s)"]
        for item in stalled:
            flag = f"stalled_{item.stage.value}"
            deal = self._crm.get_deal(item.deal_id)
            action = Action(id=f"flag_deal:{deal.id}:{flag}", type=ActionType.FLAG_DEAL, agent=self.name,
                            deal_id=deal.id, summary=f"Flag {flag}: {item.company}", payload={"flags": [flag]})
            result = self._gateway.submit(action, deal)
            if result.outcome is Outcome.EXECUTED:  # remind once per deal and stage, not on every check
                reminder = (f"Follow up with {item.contact}: no progress in {item.stage.value} "
                            f"for {item.days_in_stage:.0f} days (limit {item.limit_days})")
                self._crm.log(deal.id, "reminder", reminder)
                steps.append(f"{item.deal_id} {item.company}: {reminder}")
            else:
                steps.append(f"{item.deal_id} {item.company}: still stalled ({result.outcome.value})")
        return steps

    def _on_digest(self, event: Event) -> list[str]:
        digest = self._tracker.digest(event.now)
        self._audit.record(self.name, "digest_created", detail=digest.date.isoformat())
        return digest.render().splitlines()

    # --- Helpers ---------------------------------------------------------------

    def _submit(self, actions: list[Action]) -> list[str]:
        steps = []
        for action in actions:
            result = self._gateway.submit(action, self._crm.get_deal(action.deal_id))
            steps.append(f"{action.summary} -> {result.outcome.value}"
                         + ("" if result.outcome is Outcome.EXECUTED else f" ({result.reason})"))
            steps += self._after(action, result)
        return steps

    def _after(self, action: Action, result: ActionResult) -> list[str]:
        if result.outcome is Outcome.EXECUTED and action.type in STAGE_AFTER:
            return self._advance(action.deal_id, STAGE_AFTER[action.type])
        return []

    def _advance(self, deal_id: str, target: Stage, reason: str = "") -> list[str]:
        """Move the deal forward if the target stage's criteria are met. Otherwise log why not."""
        deal = self._crm.get_deal(deal_id)
        if not is_forward(deal.stage, target):
            return []
        missing = missing_for(target, deal, self._crm.timeline(deal_id))
        if missing:
            self._crm.log(deal_id, "note", f"Stays in {deal.stage.value}: {target.value} needs {', '.join(missing)}")
            return [f"Stays in {deal.stage.value}: {target.value} needs {', '.join(missing)}"]

        self._crm.save_deal(deal.model_copy(update={"stage": target, "stage_entered_at": utc_now()}))
        summary = f"Stage {deal.stage.value} -> {target.value}" + (f" ({reason})" if reason else "")
        self._crm.log(deal_id, "stage_change", summary, {"from": deal.stage.value, "to": target.value})
        self._audit.record(self.name, "stage_changed", deal_id=deal_id, detail=summary)
        return [summary]

    def _flag(self, deal: Deal, flag: str, detail: str) -> list[str]:
        action = Action(id=f"flag_deal:{deal.id}:{flag}", type=ActionType.FLAG_DEAL, agent=self.name,
                        deal_id=deal.id, summary=f"Flag {flag}: {detail}", payload={"flags": [flag]})
        return self._submit([action])
