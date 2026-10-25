"""Orchestrator: owns deal state and decides which agent works next.

It reacts to events, gives the right agent its task, and moves the deal
forward only when the next stage's criteria are met. Agents work in a loop
with their own tools (runtime.py): every side effect they attempt goes through
the gateway, and the orchestrator sees each outcome as it happens. Each event id is claimed before processing, so a webhook
delivered twice is handled once; if processing fails, the claim is released
so a retry can run.

The kill switch is a pause: while it is on, incoming events are held (not
processed, not lost) and approved actions wait. resume() releases the switch,
replays held events in arrival order, then runs approved actions that never
ran. Human decisions (approval.decided) and the digest are still accepted
while paused; the gateway guarantees nothing executes. A rejection is
recorded at once, but its redraft and lesson wait for the release. On replay,
an event that fails is set aside for a human (failed events, in the digest)
and the rest carry on; if the model fails, the replay stops and pauses again,
so nothing runs out of order.

    lead.created     -> research agent (enrich, score with code, why-now note) -> outreach agent
                        (Tier A waits for approval; Tier C goes to nurture)
    approval.decided -> execute the approved action; a rejected email with a note is redrafted
                        (max 2 redrafts), and the note can be saved as a lesson
    reply.received   -> reply agent: classifies (code adds pricing / injection flags), then answers a product
                        question (top tiers wait for approval), books a meeting, sets a follow-up or hands it to a
                        human; a meeting request moves the deal to Meeting and prepares a call brief for the rep; an
                        unsubscribe closes it; a reply from a lead we never emailed is flagged and doesn't move it
    call.completed   -> call intel agent -> qualified? -> deal desk drafts the IO (sending waits for approval)
    signature.completed -> verify the envelope with the e-sign provider -> close won -> handoff
    follow_up.due    -> a follow-up an agent scheduled is due: remind the rep
    sla.check        -> flag stalled deals and remind the rep (once per deal and stage)
    digest.daily     -> summary: approvals waiting, stalled and flagged deals, new lessons, stuck actions
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import Field

from revenue_agents.agents.call_brief import CallBriefAgent
from revenue_agents.agents.call_intel import CallIntelAgent, saved_fields
from revenue_agents.agents.deal_desk import DealDeskAgent, io_terms
from revenue_agents.agents.outreach import OutreachAgent, Review
from revenue_agents.agents.reply import CONTRACT_STAGES, ReplyAgent, read_classification
from revenue_agents.agents.research import ResearchAgent
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
from revenue_agents.llm import LLMError
from revenue_agents.models import Deal, DealFields, Lesson, Message, Model, Reply, Stage
from revenue_agents.runtime import AgentHost, AgentRun, AgentTask
from revenue_agents.segments import SegmentRegistry
from revenue_agents.stages import ORDER, is_forward, missing_for
from revenue_agents.storage import utc_now
from revenue_agents.tracing import NullTracer, Tracer
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
    """The AI agents (tool loops run by `host`) and the code skills (call brief, deal desk: exact on purpose)."""

    host: AgentHost
    research: ResearchAgent
    outreach: OutreachAgent
    reply: ReplyAgent
    call_intel: CallIntelAgent
    call_brief: CallBriefAgent
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
                 tracker: Tracker, esign: ESignProvider, kill_switch: KillSwitch, held: HeldEvents,
                 failed: HeldEvents, tracer: Tracer | None = None) -> None:
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
        self._failed = failed  # events that failed on replay: set aside for a human
        self._tracer = tracer or NullTracer()
        self._handlers = {
            EventType.LEAD_CREATED: self._on_lead_created,
            EventType.APPROVAL_DECIDED: self._on_approval,
            EventType.REPLY_RECEIVED: self._on_reply,
            EventType.CALL_COMPLETED: self._on_call,
            EventType.SIGNATURE_COMPLETED: self._on_signature,
            EventType.TERMS_CHANGED: self._on_terms_changed,
            EventType.FOLLOW_UP_DUE: self._on_follow_up_due,
            EventType.SLA_CHECK: self._on_sla_check,
            EventType.DIGEST_DAILY: self._on_digest,
        }

    def handle(self, event: Event) -> EventReport:
        with self._tracer.event(event.id):
            return self._handle(event)

    def _handle(self, event: Event) -> EventReport:
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
            try:
                report = self.handle(event)
            except LLMError as error:
                # The model is down: the rest would fail too. Keep them held, in order, and pause again.
                return steps + self.pause(f"Replay stopped at {event.id}: {error}", by=self.name)
            except Exception as error:
                # One bad event mustn't block the others: set it aside for a human and carry on.
                self._held.remove(event.id)
                self._failed.hold(event)
                steps.append(f"{event.type.value} {event.id}: failed ({type(error).__name__}: {error}); "
                             "set aside for a human, the rest carries on")
                continue
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
        with self._tracer.event(f"run_now:{request_id}"):
            return self._execute_approved(request)

    # --- Event handlers --------------------------------------------------------

    def _on_lead_created(self, event: Event) -> list[str]:
        lead = self._crm.get_lead(event.payload["lead_id"])
        deal = self._crm.deal_for_lead(lead.id)
        steps = [f"New lead: {lead.company} ({lead.segment}) -> deal {deal.id}"]

        _, lines = self._work(self._agents.research.task(lead, deal))
        steps += lines
        steps += self._advance(deal.id, Stage.ENRICHED)
        deal = self._crm.get_deal(deal.id)
        if deal.enrichment is None:
            return steps + ["Stopped: lead could not be enriched"]

        steps += self._advance(deal.id, Stage.SCORED)
        deal = self._crm.get_deal(deal.id)
        if deal.score is None:
            return steps + ["Stopped: lead could not be scored"]

        if deal.score.tier not in self._segments.get(lead.segment).messaging.outreach_tiers:
            return steps + [f"Tier {deal.score.tier.value}: no outreach, left for nurture"]
        return steps + self._first_email(lead, deal)

    def _first_email(self, lead, deal: Deal, review: Review | None = None) -> list[str]:
        """The outreach agent drafts, checks and sends. If it ends without an acceptable draft, a human writes it."""
        run, lines = self._work(self._agents.outreach.first_touch(lead, deal, review))
        sends = [call for call in run.called("send_email") if call.action is not None]
        if not sends:
            drafts = run.called("draft_email")
            problem = drafts[-1].result if drafts else (run.summary or "no draft was written")
            return lines + self._flag(deal, "outreach_draft_rejected", f"No acceptable draft for {lead.company}: "
                                                                       f"{problem}")
        used = sends[-1].action.payload.get("lessons_used")
        return ([f"Draft uses lessons: {', '.join(used)}"] if used else []) + lines

    def _on_approval(self, event: Event) -> list[str]:
        request_id, reviewer, approved = event.payload["request_id"], event.payload["reviewer"], event.payload["approved"]
        request = self._approvals.get(request_id)
        steps = []
        if request.status is ApprovalStatus.PENDING:
            edits = _real_edits(request, event.payload.get("edits")) if approved else {}
            if edits:
                request = self._approvals.approve(request_id, reviewer, event.payload.get("note"), edits)
                detail = f"{reviewer} edited the {' and '.join(sorted(edits))} before approving"
                self._audit.record(self.name, "edited_by_reviewer", deal_id=request.action.deal_id,
                                   action_id=request.id, detail=detail)
                steps.append(f"Edited by {reviewer} before sending ({' and '.join(sorted(edits))})")
            else:
                decide = self._approvals.approve if approved else self._approvals.reject
                request = decide(request_id, reviewer, event.payload.get("note"))
        elif (request.status is ApprovalStatus.APPROVED) != approved:
            return [f"Already {request.status.value} by {request.decided_by}: decision not changed"]

        if not approved:
            if self._kill_switch.is_engaged() and (request.note or "").strip():
                # The decision stands now; the redraft and the lesson (model calls, side effects) wait for release.
                later = event.model_copy(update={"id": f"{event.id}:after_pause"})
                return steps + [f"Rejected by {request.decided_by}: {request.id}"] + self._hold(
                    later, "Agents paused").steps
            return self._after_rejection(request, event.payload.get("save_lesson", False))
        # Same decision again (e.g. a retry after a failure): carry on and run it. Actions run once.
        return steps + self._execute_approved(request)

    def _after_rejection(self, request: ApprovalRequest, save_lesson: bool) -> list[str]:
        note = (request.note or "").strip()
        steps = [f"Rejected by {request.decided_by}: {request.id}" + (f' ("{note}")' if note else "")]
        if note and save_lesson:
            steps += self._lesson_from_review(request, note)
        if note and request.action.type is ActionType.SEND_EMAIL:
            steps += self._redraft(request, note)
        if request.action.payload.get("requested_changes"):
            steps.append("The customer's requested change was declined: the IO already sent stays valid")
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
        return [f"Redrafting with the reviewer's feedback (v{version})"] + self._first_email(lead, deal, review)

    def _execute_approved(self, request: ApprovalRequest) -> list[str]:
        deal = self._crm.get_deal(request.action.deal_id)
        result = self._gateway.execute_approved(request.id, deal)
        line = f"Approved by {request.decided_by}: {request.action.summary} -> {result.outcome.value}"
        if result.outcome is Outcome.BLOCKED:
            line += f" ({result.reason}; it will run when the kill switch is released, or via Run now)"
        steps = [line] + self._after(request.action, result)
        if result.outcome is Outcome.EXECUTED and request.action.type is ActionType.SEND_IO:
            steps += self._after_io_sent(request.action)
        return steps

    def _on_reply(self, event: Event) -> list[str]:
        reply = Reply.model_validate(event.payload["reply"])
        lead = self._crm.get_lead(reply.lead_id)
        deal = self._crm.deal_for_lead(lead.id)
        answers = self._email_answered_by(reply, deal)

        def replied(action: Action, result: ActionResult) -> list[str]:
            # Classified, and it answers one of our emails: the deal is Replied from here, so an answer the
            # agent drafts next is treated as a reply to this lead (top tiers wait for a human).
            if answers and action.type is ActionType.CLASSIFY_REPLY and result.outcome is Outcome.EXECUTED:
                return self._advance(deal.id, Stage.REPLIED)
            return []

        run, lines = self._work(self._agents.reply.task(reply, lead, deal, answers), also=replied)
        reading = read_classification(run)
        intent = reading[0].intent if reading else "not classified"
        flags = reading[1] if reading else []
        steps = [f"Reply {reply.id} from {lead.contact.name}: {intent}"]
        if flags:
            steps.append(f"Flags: {', '.join(flags)}")
        meta = {"intent": intent, "flags": flags}
        if reading:
            meta.update(summary=reading[0].summary, suggested_next_step=reading[0].suggested_next_step)
        self._crm.add_message(Message(
            id=f"msg:reply:{reply.id}", thread_id=answers.thread_id if answers else f"T-{deal.id}", deal_id=deal.id,
            direction="inbound", sender=reply.sender, to=answers.sender if answers else "", subject=reply.subject,
            body=reply.body, at=reply.received_at, in_reply_to=answers.id if answers else None, meta=meta,
        ))
        if answers:
            steps.append(f"Linked to our email {answers.id} in thread {answers.thread_id}")
        answered = [call for call in run.called("answer_question") if call.action is not None]
        if answered and reading and reading[0].question:
            steps.append(f"Question to answer: {reading[0].question}")
            used = answered[-1].action.payload.get("lessons_used")
            if used:
                steps.append(f"Answer uses lessons: {', '.join(used)}")
        steps += lines

        if reading is None:
            return steps + self._flag(deal, "reply_needs_human", f"the reply agent didn't classify {reply.id}")
        classification = reading[0]

        if answers is None:
            # We never emailed this lead, so this can't be a reply to us: a data error, a misrouted
            # thread, or a spoofed message. Record and flag it, learn nothing, don't move the deal.
            steps += self._flag(deal, "reply_without_outreach", f"{reply.id} arrived but we never emailed {lead.contact.name}")
            if "unsubscribe" in flags:  # always honour an unsubscribe
                steps += self._advance(deal.id, Stage.CLOSED_LOST, reason="contact unsubscribed")
            return steps + ["Reply without outreach: flagged for a human, stage unchanged, nothing learned"]

        steps += self._advance(deal.id, Stage.REPLIED)
        if "unsubscribe" in flags:
            return steps + self._advance(deal.id, Stage.CLOSED_LOST, reason="contact unsubscribed")
        if deal.stage in CONTRACT_STAGES and classification.intent in ("objection", "interested", "other"):
            changes = {change.field: change.value for change in classification.contract_changes}
            if deal.stage is Stage.SIGNATURE and changes and "possible_prompt_injection" not in flags:
                # The customer asked for specific changes to the IO they received: draft them for the rep to approve.
                return steps + self._propose_io_change(lead, deal.id, changes, reply)
            # Otherwise a person reads it: flag it; the rep updates the terms and the IO is redrafted.
            return steps + self._flag(self._crm.get_deal(deal.id), "io_change_requested",
                                      f"{lead.contact.name} wrote about the IO: {classification.summary}")
        if "meeting_requested" in self._crm.get_deal(deal.id).flags:
            steps += self._advance(deal.id, Stage.MEETING)
            if self._crm.get_deal(deal.id).stage is Stage.MEETING:
                steps += self._prepare_call_brief(lead, deal.id)
        if "pricing_requested" in flags:
            steps.append("Pricing question: a human must answer (pricing always needs approval)")
        if "possible_prompt_injection" in flags:
            steps.append("Suspicious content: ignored as instructions, no lesson learned")
        return steps

    def _email_answered_by(self, reply: Reply, deal: Deal) -> Message | None:
        """Which of our emails this reply answers: the exact one if the reply names it (In-Reply-To),
        otherwise the latest email we sent on this deal. None means we never emailed them."""
        sent = [message for message in self._crm.messages(deal.id) if message.direction == "outbound"]
        exact = next((message for message in sent if message.id == reply.in_reply_to), None)
        return exact or (sent[-1] if sent else None)

    def _prepare_call_brief(self, lead, deal_id: str, after_call: str | None = None) -> list[str]:
        """Before a meeting: a prep sheet with what we know, lessons from similar deals, and what to ask.
        After a call that left things open, the same sheet for the next call."""
        deal = self._crm.get_deal(deal_id)
        lessons = self._lessons.for_segment(lead.segment, purpose="call_brief")
        result = self._agents.call_brief.run(lead, deal, self._crm.timeline(deal_id), lessons, after_call=after_call)
        return self._submit(result.actions)

    def _on_call(self, event: Event) -> list[str]:
        lead = self._crm.get_lead(event.payload["lead_id"])
        deal = self._crm.deal_for_lead(lead.id)
        transcript_id, transcript = event.payload["transcript_id"], event.payload["transcript"]
        # Keep the call on the deal, as HubSpot keeps a call recording: the rep can read what was actually said.
        self._crm.log(deal.id, "call", f"Call recorded: {transcript_id}",
                      {"transcript_id": transcript_id, "transcript": transcript})
        run, lines = self._work(self._agents.call_intel.task(transcript_id, transcript, lead, deal))
        fields = saved_fields(run)
        steps = [f"Call {transcript_id}: budget {fields.budget_usd if fields else None}, "
                 f"signer {fields.decision_maker if fields else None}"]
        steps += [f"Lesson learned: {action.payload['lesson']['text']}" for action, result in run.results()
                  if action.type is ActionType.RECORD_LESSON and result.outcome is Outcome.EXECUTED]
        steps += lines
        if fields is None:
            steps += self._flag(self._crm.get_deal(deal.id), "call_needs_review",
                                f"the call intel agent saved nothing from {transcript_id}")

        steps += self._advance(deal.id, Stage.QUALIFIED)
        deal = self._crm.get_deal(deal.id)
        if deal.stage is Stage.CLOSED_LOST or ORDER.index(deal.stage) < ORDER.index(Stage.QUALIFIED):
            steps.append("Not qualified yet: no IO drafted")
            if deal.stage is Stage.MEETING:  # another call is needed: prepare the rep for it
                steps += self._prepare_call_brief(lead, deal.id, after_call=transcript_id)
            return steps
        if deal.stage is Stage.HANDOFF:
            return steps + ["Deal already signed: IO not redrafted"]
        return steps + self._draft_io(lead, deal.id, reason=f"call {transcript_id}")

    def _on_terms_changed(self, event: Event) -> list[str]:
        """A rep changed the commercial terms. Update the deal (audited), then redraft the IO from the new facts."""
        deal = self._crm.get_deal(event.payload["deal_id"])
        lead = self._crm.get_lead(deal.lead_id)
        changes, by = event.payload["changes"], event.payload["by"]
        unknown = set(changes) - EDITABLE_TERMS
        if unknown:
            raise ValueError(f"these terms can't be changed here: {', '.join(sorted(unknown))}")
        DealFields.model_validate({**deal.fields.model_dump(), **changes})  # invalid values fail before anything changes

        described = ", ".join(f"{name.replace('_', ' ')} → {value}" for name, value in changes.items())
        update = Action(id=f"update_deal:{deal.id}:{event.id}", type=ActionType.UPDATE_DEAL, agent=self.name,
                        deal_id=deal.id, summary=f"Terms changed by {by}: {described}",
                        payload={"fields": changes, "changed_by": by, "clear_flags": ["io_change_requested"]})
        steps = self._submit([update])
        return steps + self._draft_io(lead, deal.id, reason=f"terms changed by {by}")

    def _draft_io(self, lead, deal_id: str, reason: str) -> list[str]:
        """Draft the deal's IO, or a new version if the terms differ from the latest one.
        A newer version replaces the old: a pending one is closed, a sent one is voided so it can't be signed."""
        deal = self._crm.get_deal(deal_id)
        drafts = [a for a in self._crm.timeline(deal_id) if a.kind == "document"]
        if drafts and drafts[-1].data.get("terms") == io_terms(deal.fields):
            return [f"IO terms unchanged: {drafts[-1].data['io_number']} still stands"]

        version = len(drafts) + 1
        io = self._agents.deal_desk.run(lead, deal, version=version)
        if io.output.errors:
            return self._flag(deal, "io_blocked", "; ".join(io.output.errors))
        steps = [f"Redrafting the IO as {io.output.io_number} ({reason})"] if drafts else []
        steps += self._replace_old_io(deal, io.output.io_number)
        return steps + self._submit(io.actions)

    def _propose_io_change(self, lead, deal_id: str, changes: dict, reply: Reply) -> list[str]:
        """The customer asked to change the IO they received. Draft the new version for the rep to approve.
        Nothing is final yet: the deal keeps its terms and the sent IO stays valid until the rep approves."""
        deal = self._crm.get_deal(deal_id)
        unknown = set(changes) - EDITABLE_TERMS
        try:
            proposed = DealFields.model_validate({**deal.fields.model_dump(), **changes})
        except ValueError as error:
            return self._flag(deal, "io_change_requested", f"{reply.id} asks for changes we couldn't read: {error}")
        if unknown:
            return self._flag(deal, "io_change_requested", f"{reply.id} asks to change {', '.join(sorted(unknown))}")
        if io_terms(proposed) == io_terms(deal.fields):
            return [f"{reply.id} asks for terms the IO already has: nothing to redraft"]

        drafts = [a for a in self._crm.timeline(deal_id) if a.kind == "document"]
        io = self._agents.deal_desk.run(lead, deal.model_copy(update={"fields": proposed}), version=len(drafts) + 1)
        if io.output.errors:
            return self._flag(deal, "io_change_requested", f"{reply.id}: the requested IO isn't valid: "
                                                          f"{'; '.join(io.output.errors)}")
        request = {"requested_changes": {name: proposed.model_dump(mode="json")[name] for name in changes},
                   "requested_in": reply.id}
        actions = [action.model_copy(update={"payload": {**action.payload, **request}}) for action in io.actions]
        described = ", ".join(f"{name.replace('_', ' ')} → {value}" for name, value in changes.items())
        steps = [f"{lead.contact.name} asked to change the IO ({described}): drafting {io.output.io_number} "
                 f"for your approval. The current IO stays valid until you approve."]
        for pending in self._approvals.pending():  # an older draft that was never sent is simply replaced
            if pending.action.deal_id == deal_id and pending.action.type is ActionType.SEND_IO:
                self._approvals.reject(pending.id, self.name, note=f"Replaced by {io.output.io_number}")
        return steps + self._submit(actions)

    def _after_io_sent(self, action: Action) -> list[str]:
        """A newer IO went out: the customer's requested terms become the deal's, and older IOs are voided."""
        deal = self._crm.get_deal(action.deal_id)
        steps = []
        requested = action.payload.get("requested_changes")
        if requested:
            update = Action(id=f"update_deal:{deal.id}:{action.payload['io_number']}", type=ActionType.UPDATE_DEAL,
                            agent=self.name, deal_id=deal.id,
                            summary=f"Terms agreed in {action.payload['io_number']} (asked in "
                                    f"{action.payload['requested_in']})",
                            payload={"fields": requested, "clear_flags": ["io_change_requested"]})
            steps += self._submit([update])
        for sent in [a for a in self._crm.timeline(deal.id) if a.kind == "io_sent"]:
            envelope = self._esign.get(sent.data["envelope_id"])
            if envelope.status is EnvelopeStatus.SENT and envelope.io_number != action.payload["io_number"]:
                void = Action(id=f"void_io:{deal.id}:{envelope.id}", type=ActionType.VOID_IO, agent=self.name,
                              deal_id=deal.id, summary=f"Void {envelope.io_number}: replaced by "
                                                       f"{action.payload['io_number']}",
                              payload={"envelope_id": envelope.id, "reason": f"replaced by {action.payload['io_number']}"})
                steps += self._submit([void])
        return steps

    def _replace_old_io(self, deal: Deal, new_io: str) -> list[str]:
        steps = []
        for request in self._approvals.pending():
            if request.action.deal_id == deal.id and request.action.type is ActionType.SEND_IO:
                self._approvals.reject(request.id, self.name, note=f"Replaced by {new_io}")
                self._audit.record(self.name, "io_replaced", deal_id=deal.id, action_id=request.id,
                                   detail=f"replaced by {new_io} before it was sent")
                steps.append(f"{request.action.payload['io_number']} was waiting for approval: replaced by {new_io}")
        for sent in [a for a in self._crm.timeline(deal.id) if a.kind == "io_sent"]:
            envelope = self._esign.get(sent.data["envelope_id"])
            if envelope.status is EnvelopeStatus.SENT:
                void = Action(id=f"void_io:{deal.id}:{envelope.id}", type=ActionType.VOID_IO, agent=self.name,
                              deal_id=deal.id, summary=f"Void {envelope.io_number}: replaced by {new_io}",
                              payload={"envelope_id": envelope.id, "reason": f"replaced by {new_io}"})
                steps += self._submit([void])
        return steps

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
        if envelope.status is EnvelopeStatus.VOIDED:
            return self._flag(deal, "signature_on_voided_io", f"{envelope.id} was replaced by a newer IO")
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

    def _on_follow_up_due(self, event: Event) -> list[str]:
        """A follow-up an agent scheduled is due: remind the rep, once."""
        deal = self._crm.get_deal(event.payload["deal_id"])
        lead = self._crm.get_lead(deal.lead_id)
        reminder = f"Follow up with {lead.contact.name} today: {event.payload['reason']}"
        self._crm.log(deal.id, "reminder", reminder, {"due": event.payload.get("due")})
        return [f"{deal.id} {lead.company}: {reminder}"] + self._flag(deal, "follow_up_due", event.payload["reason"])

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

    def _work(self, task: AgentTask, also: Callable[[Action, ActionResult], list[str]] | None = None
              ) -> tuple[AgentRun, list[str]]:
        """Run an agent's task. Each side effect it attempts is reported (and moves stages) as it happens."""
        lines: list[str] = []

        def on_result(action: Action, result: ActionResult) -> None:
            lines.append(_line(action, result))
            lines.extend(self._after(action, result))
            if also:
                lines.extend(also(action, result))

        return self._agents.host.run(task, on_result), lines

    def _submit(self, actions: list[Action]) -> list[str]:
        steps = []
        for action in actions:
            result = self._gateway.submit(action, self._crm.get_deal(action.deal_id))
            steps.append(_line(action, result))
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
            self._tracer.record("stage", target.value, {"deal": deal_id, "from": deal.stage.value, "to": deal.stage.value,
                                                         "why": f"not moved: {target.value} needs {', '.join(missing)}"})
            self._crm.log(deal_id, "note", f"Stays in {deal.stage.value}: {target.value} needs {', '.join(missing)}")
            return [f"Stays in {deal.stage.value}: {target.value} needs {', '.join(missing)}"]

        self._crm.save_deal(deal.model_copy(update={"stage": target, "stage_entered_at": utc_now()}))
        summary = f"Stage {deal.stage.value} -> {target.value}" + (f" ({reason})" if reason else "")
        self._tracer.record("stage", target.value, {"deal": deal_id, "from": deal.stage.value, "to": target.value,
                                                     "why": reason or "exit criteria met"})
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


def _line(action: Action, result: ActionResult) -> str:
    return f"{action.summary} -> {result.outcome.value}" + ("" if result.outcome is Outcome.EXECUTED else f" ({result.reason})")


# A reviewer may rewrite an email's wording before approving it; nothing else about an action can be edited.
EDITABLE_EMAIL_FIELDS = frozenset({"subject", "body"})


def _real_edits(request: ApprovalRequest, edits: dict | None) -> dict[str, str]:
    """The reviewer's actual changes, checked. Raises (so nothing is approved) if they aren't allowed."""
    if not edits:
        return {}
    if request.action.type is not ActionType.SEND_EMAIL:
        raise ValueError(f"only emails can be edited before approval, not {request.action.type.value}")
    unknown = set(edits) - EDITABLE_EMAIL_FIELDS
    if unknown:
        raise ValueError(f"only the subject and body can be edited, not {', '.join(sorted(unknown))}")
    cleaned = {field: str(value).strip() for field, value in edits.items()}
    if not all(cleaned.values()):
        raise ValueError("an edited subject or body can't be empty")
    return {field: value for field, value in cleaned.items() if value != request.action.payload.get(field, "").strip()}


# Commercial terms a rep may change from the UI; each change redrafts the IO.
EDITABLE_TERMS = frozenset({"budget_usd", "flight_start", "flight_end", "payment_terms", "contract_term", "special_terms",
                            "scope"})
