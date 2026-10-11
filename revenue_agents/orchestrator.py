"""Orchestrator: owns deal state and decides which agent works next.

It reacts to events, runs the right agents, sends every proposed action
through the gateway, and moves the deal forward only when the next stage's
criteria are met. Each event id is claimed before processing, so a webhook
delivered twice is handled once; if processing fails, the claim is released
so a retry can run.

The kill switch is a pause: while it is on, incoming events are held (not
processed, not lost) and approved actions wait. resume() releases the switch,
replays held events in arrival order, then runs approved actions that never
ran. Human decisions (approval.decided) and the digest are still accepted
while paused; the gateway guarantees nothing executes.

    lead.created     -> research -> scoring -> outreach (Tier A waits for approval)
    approval.decided -> execute the approved action; a rejected email with a note is redrafted
                        (max 2 redrafts), and the note can be saved as a lesson
    reply.received   -> reply triage -> flags (meeting / pricing / injection / unsubscribe); a reply from a
                        lead we never emailed is flagged and doesn't move the deal
    call.completed   -> call intel -> qualified? -> deal desk drafts IO (sending waits for approval)
    signature.completed -> verify the envelope with the e-sign provider -> close won -> handoff
    sla.check        -> flag stalled deals and remind the rep (once per deal and stage)
    digest.daily     -> summary: approvals waiting, stalled and flagged deals, new lessons, stuck actions
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from revenue_agents.agents.call_intel import CallIntelAgent
from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.agents.outreach import DraftRejectedError, OutreachAgent, Review
from revenue_agents.agents.reply_triage import ReplyTriageAgent
from revenue_agents.agents.research import ResearchAgent
from revenue_agents.agents.scoring import ScoringAgent
from revenue_agents.crm import CRM
from revenue_agents.esign import EnvelopeStatus, ESignProvider
from revenue_agents.events import Event, EventType
from revenue_agents.guardrails.approvals import ApprovalQueue, ApprovalRequest, ApprovalStatus
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.gateway import ActionGateway, ActionResult, Outcome
from revenue_agents.guardrails.held_events import HeldEvents
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import Action, ActionType
from revenue_agents.lessons import LessonStore
from revenue_agents.models import Deal, Lesson, Model, Reply, Stage
from revenue_agents.segments import SegmentRegistry
from revenue_agents.stages import ORDER, is_forward, missing_for
from revenue_agents.storage import utc_now
from revenue_agents.tracking import Tracker

# Accepted even while the kill switch is on: human decisions are recorded (the gateway still blocks
# execution), and the digest only reports.
ACCEPTED_WHILE_PAUSED = frozenset({EventType.APPROVAL_DECIDED, EventType.DIGEST_DAILY})

# A first email can be redrafted twice after rejection; after that a human writes it.
MAX_DRAFTS = 3

# Executing these actions is the evidence for the next stage.
STAGE_AFTER = {ActionType.SEND_EMAIL: Stage.OUTREACH, ActionType.DRAFT_IO: Stage.PROPOSAL,
               ActionType.SEND_IO: Stage.SIGNATURE, ActionType.CLOSE_DEAL: Stage.HANDOFF}


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
    status: str  # "processed", "duplicate" or "held"
    steps: list[str] = Field(default_factory=list)


class Orchestrator:
    name = "orchestrator"

    def __init__(self, crm: CRM, lessons: LessonStore, gateway: ActionGateway, approvals: ApprovalQueue,
                 audit: AuditLog, segments: SegmentRegistry, agents: Agents, events: ActionClaims,
                 tracker: Tracker, esign: ESignProvider, kill_switch: KillSwitch, held: HeldEvents) -> None:
        self._crm = crm
        self._lessons = lessons
        self._gateway = gateway
        self._approvals = approvals
        self._audit = audit
        self._segments = segments
        self._agents = agents
        self._events = events
        self._tracker = tracker
        self._esign = esign
        self._kill_switch = kill_switch
        self._held = held
        self._handlers = {
            EventType.LEAD_CREATED: self._on_lead_created,
            EventType.APPROVAL_DECIDED: self._on_approval,
            EventType.REPLY_RECEIVED: self._on_reply,
            EventType.CALL_COMPLETED: self._on_call,
            EventType.SIGNATURE_COMPLETED: self._on_signature,
            EventType.SLA_CHECK: self._on_sla_check,
            EventType.DIGEST_DAILY: self._on_digest,
        }

    def handle(self, event: Event) -> EventReport:
        if not self._events.claim(event.id):
            self._audit.record(self.name, "event_duplicate", action_id=event.id, detail=event.type.value)
            return EventReport(event_id=event.id, status="duplicate", steps=["Already handled: ignored"])

        if self._kill_switch.is_engaged() and event.type not in ACCEPTED_WHILE_PAUSED:
            self._events.release(event.id)
            return self._hold(event, "Kill switch on")

        self._audit.record(self.name, "event_received", action_id=event.id, detail=event.type.value)
        try:
            steps = self._handlers[event.type](event)
        except Exception as error:
            self._events.release(event.id)  # let a retry process it
            self._audit.record(self.name, "event_failed", action_id=event.id, detail=f"{type(error).__name__}: {error}")
            raise

        if self._kill_switch.is_engaged() and event.type not in ACCEPTED_WHILE_PAUSED:
            # Pulled while this event was being processed: replay it on release. Actions that already ran
            # are skipped then (their claims), so the replay finishes the work without repeating it.
            self._events.release(event.id)
            return self._hold(event, "Kill switch pulled during processing", steps)

        self._events.complete(event.id)
        return EventReport(event_id=event.id, status="processed", steps=steps)

    # --- Kill switch -------------------------------------------------------------

    def pause(self, reason: str, by: str) -> list[str]:
        self._kill_switch.engage(f"{reason} (by {by})")
        self._audit.record(self.name, "kill_switch_engaged", detail=f"{reason} (by {by})")
        return [f"Kill switch ON: {reason}. New events will be held; nothing will execute."]

    def resume(self, by: str) -> list[str]:
        """Release the kill switch, replay held events in arrival order, then run approved actions that waited."""
        self._kill_switch.release()
        self._audit.record(self.name, "kill_switch_released", detail=f"by {by}")
        steps = [f"Kill switch OFF (by {by}). Replaying {len(self._held.all())} held event(s)."]
        for event in self._held.all():
            report = self.handle(event)
            self._held.remove(event.id)
            steps += [f"{event.type.value} {event.id}:"] + [f"  {line}" for line in report.steps]
        for request in self.approved_waiting():
            steps += self.run_approved(request.id)
        return steps

    def held_events(self) -> list[Event]:
        return self._held.all()

    def approved_waiting(self) -> list[ApprovalRequest]:
        """Approved by a human but never executed (kill switch, or a failure after the decision)."""
        return self._tracker.approved_waiting()

    def run_approved(self, request_id: str) -> list[str]:
        """Execute an approved action that hasn't run yet ("Run now"). Safe to repeat: actions run once."""
        request = self._approvals.get(request_id)
        if request.status is not ApprovalStatus.APPROVED:
            return [f"{request_id} is {request.status.value}: nothing to run"]
        return self._execute_approved(request)

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
        request_id, reviewer, approved = event.payload["request_id"], event.payload["reviewer"], event.payload["approved"]
        request = self._approvals.get(request_id)
        if request.status is ApprovalStatus.PENDING:
            decide = self._approvals.approve if approved else self._approvals.reject
            request = decide(request_id, reviewer, event.payload.get("note"))
        elif (request.status is ApprovalStatus.APPROVED) != approved:
            return [f"Already {request.status.value} by {request.decided_by}: decision not changed"]

        if not approved:
            return self._after_rejection(request, event.payload.get("save_lesson", False))
        # Same decision again (e.g. a retry after a failure): carry on and run it. Actions run once.
        return self._execute_approved(request)

    def _after_rejection(self, request: ApprovalRequest, save_lesson: bool) -> list[str]:
        note = (request.note or "").strip()
        steps = [f"Rejected by {request.decided_by}: {request.id}" + (f' ("{note}")' if note else "")]
        if note and save_lesson:
            steps += self._lesson_from_review(request, note)
        if note and request.action.type is ActionType.SEND_EMAIL:
            steps += self._redraft(request, note)
        return steps

    def _lesson_from_review(self, request: ApprovalRequest, note: str) -> list[str]:
        deal = self._crm.get_deal(request.action.deal_id)
        lesson = Lesson(id=f"lesson:review:{request.id}", segment=deal.segment, source="review",
                        source_id=request.id, text=note, created_at=utc_now())
        action = Action(id=f"record_lesson:{deal.id}:{lesson.id}", type=ActionType.RECORD_LESSON, agent=self.name,
                        deal_id=deal.id, summary=f"Lesson from {request.decided_by}'s review: {note}",
                        payload={"lesson": lesson.model_dump(mode="json")})
        return self._submit([action])

    def _redraft(self, request: ApprovalRequest, note: str) -> list[str]:
        """The outreach agent writes the next version with the reviewer's note. It still needs approval."""
        deal = self._crm.get_deal(request.action.deal_id)
        rejected = [r for r in self._approvals.all() if r.action.deal_id == deal.id
                    and r.action.type is ActionType.SEND_EMAIL and r.status is ApprovalStatus.REJECTED]
        version = len(rejected) + 1
        if version > MAX_DRAFTS:
            return self._flag(deal, "needs_manual_email", f"{len(rejected)} drafts rejected; a human should write it")

        lead = self._crm.get_lead(deal.lead_id)
        review = Review(feedback=note, previous_body=request.action.payload["body"], next_version=version)
        lessons = self._lessons.for_segment(lead.segment, purpose="outreach")
        try:
            result = self._agents.outreach.run(lead, deal, deal.enrichment, lessons, review=review)
        except DraftRejectedError as error:
            return self._flag(deal, "outreach_draft_rejected", str(error))
        return [f"Redrafting with the reviewer's feedback (v{version})"] + self._submit(result.actions)

    def _execute_approved(self, request: ApprovalRequest) -> list[str]:
        deal = self._crm.get_deal(request.action.deal_id)
        result = self._gateway.execute_approved(request.id, deal)
        line = f"Approved by {request.decided_by}: {request.action.summary} -> {result.outcome.value}"
        if result.outcome is Outcome.BLOCKED:
            line += f" ({result.reason}; it will run when the kill switch is released, or via Run now)"
        return [line] + self._after(request.action, result)

    def _on_reply(self, event: Event) -> list[str]:
        reply = Reply.model_validate(event.payload["reply"])
        lead = self._crm.get_lead(reply.lead_id)
        deal = self._crm.deal_for_lead(lead.id)
        result = self._agents.reply_triage.run(reply, lead, deal)
        triage = result.output
        steps = [f"Reply {reply.id} from {lead.contact.name}: {triage.classification.intent}"]
        if triage.flags:
            steps.append(f"Flags: {', '.join(triage.flags)}")

        emailed = any(activity.kind == "email_sent" for activity in self._crm.timeline(deal.id))
        if not emailed:
            # We never emailed this lead, so this can't be a reply to us: a data error, a misrouted
            # thread, or a spoofed message. Record and flag it, learn nothing, don't move the deal.
            actions = [a for a in result.actions if a.type is not ActionType.RECORD_LESSON]
            steps += self._submit(actions)
            steps += self._flag(deal, "reply_without_outreach", f"{reply.id} arrived but we never emailed {lead.contact.name}")
            if "unsubscribe" in triage.flags:  # always honour an unsubscribe
                steps += self._advance(deal.id, Stage.CLOSED_LOST, reason="contact unsubscribed")
            return steps + ["Reply without outreach: flagged for a human, stage unchanged, nothing learned"]

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

    def _on_signature(self, event: Event) -> list[str]:
        """Close the deal only if the provider confirms a signed envelope that this deal is waiting for."""
        envelope_id = event.payload.get("envelope_id", "")
        try:
            envelope = self._esign.get(envelope_id)  # ask the provider; don't trust the webhook body
        except KeyError:
            self._audit.record(self.name, "signature_rejected", action_id=event.id,
                               detail=f"unknown envelope {envelope_id!r}")
            return [f"Rejected: unknown envelope {envelope_id!r} (forged or misrouted webhook)"]

        deal = self._crm.get_deal(envelope.deal_id)
        sent = [a for a in self._crm.timeline(deal.id) if a.kind == "io_sent" and a.data.get("envelope_id") == envelope.id]
        if envelope.status is not EnvelopeStatus.SIGNED:
            return self._flag(deal, "signature_unconfirmed", f"provider says {envelope.id} is {envelope.status.value}")
        if not sent or deal.stage is not Stage.SIGNATURE:
            return self._flag(deal, "signature_mismatch", f"{envelope.id} doesn't match a pending signature")

        self._crm.log(deal.id, "signed", f"{envelope.io_number} signed by {envelope.signed_by}",
                      {"envelope_id": envelope.id, "signed_by": envelope.signed_by})
        steps = [f"{envelope.io_number} signed by {envelope.signed_by} (verified with the e-sign provider)"]
        close = Action(id=f"close_deal:{deal.id}:won", type=ActionType.CLOSE_DEAL, agent=self.name, deal_id=deal.id,
                       summary=f"Closed won: {envelope.io_number}, handoff to delivery",
                       payload={"signed_by": envelope.signed_by, "envelope_id": envelope.id})
        return steps + self._submit([close])

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

    def _hold(self, event: Event, why: str, steps: list[str] | None = None) -> EventReport:
        self._held.hold(event)
        self._audit.record(self.name, "event_held", action_id=event.id, detail=f"{why}: {event.type.value}")
        return EventReport(event_id=event.id, status="held",
                           steps=(steps or []) + [f"{why}: {event.type.value} held until release, nothing lost"])

    def _flag(self, deal: Deal, flag: str, detail: str) -> list[str]:
        action = Action(id=f"flag_deal:{deal.id}:{flag}", type=ActionType.FLAG_DEAL, agent=self.name,
                        deal_id=deal.id, summary=f"Flag {flag}: {detail}", payload={"flags": [flag]})
        return self._submit([action])
