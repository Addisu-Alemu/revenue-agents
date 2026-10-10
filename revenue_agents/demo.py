"""The live demo: one segment's leads, replies and calls, run through the real system.

The story is driven by the data, not by segment-specific code:
  1. the first lead that has a call transcript: lead -> its replies -> its call -> IO
  2. every other lead in the segment, then their replies
  3. a few days later: the scheduled SLA check and daily digest
So the lesson learned on the call is available when the later emails are drafted.

High-risk actions pause for a human (or are approved automatically with
--auto-approve). Fake mode is always announced as scripted.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from collections.abc import Callable
from pathlib import Path

from revenue_agents.events import Event, EventType
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.llm import LLMError
from revenue_agents.models import Lead, Reply
from revenue_agents.segments import SegmentConfigError, SegmentNotFoundError
from revenue_agents.storage import STATE_DIR, utc_now
from revenue_agents.system import System, build_system, make_llm

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEMO_STATE = STATE_DIR / "demo"
REVIEWER = "demo-reviewer@cool.example"
IO_SUMMARY_LINES = ("# Insertion Order", "| Signatory", "**Flight dates:**", "**Total budget:**", "**Payment terms:**")
GREETINGS = ("hi ", "hello", "dear ", "hey ")

Out = Callable[[str], None]
Ask = Callable[[str], str]


def run(segment: str = "brand", llm_mode: str = "fake", state_dir: Path = DEMO_STATE, auto_approve: bool = False,
        fresh: bool = True, out: Out = print, ask: Ask = input) -> int:
    state_dir = Path(state_dir)
    if fresh and state_dir.exists():
        shutil.rmtree(state_dir)

    out(f"Revenue Agents demo | segment: {segment} | model: {llm_mode}")
    if llm_mode == "fake":
        out("NOTE: fake mode. Model answers are scripted for this mock data; everything else is the real system.")
    system = build_system(make_llm(llm_mode), state_dir=state_dir)

    try:
        pack = system.segments.get(segment)
    except (SegmentNotFoundError, SegmentConfigError) as error:
        out(f"Cannot run segment '{segment}': {error}")
        return 1
    out(f"Segment pack: {pack.display_name}")

    demo = _Demo(system, out, ask, auto_approve)
    try:
        demo.story(segment)
    except LLMError as error:
        out(f"FAILED: {error}")
        return 1
    demo.summary()
    return 0


class _Demo:
    def __init__(self, system: System, out: Out, ask: Ask, auto_approve: bool) -> None:
        self.system, self.out, self.ask, self.auto_approve = system, out, ask, auto_approve
        self.act = 0
        self.previewed: set[str] = set()  # deals whose email was already shown in an approval

    def story(self, segment: str) -> None:
        leads = sorted((lead for lead in self.system.crm.leads() if lead.segment == segment), key=lambda lead: lead.id)
        replies = [Reply.model_validate(r) for r in json.loads((DATA_DIR / "replies.json").read_text())]
        transcripts = {path.name.split("_")[0]: path for path in sorted((DATA_DIR / "transcripts").glob("*.txt"))}

        first = next((lead for lead in leads if lead.id in transcripts), None)
        if first:
            self.lead_created(first)
            for reply in (r for r in replies if r.lead_id == first.id):
                self.reply(reply, show_duplicate=True)
            self.call(first, transcripts[first.id])

        for lead in (lead for lead in leads if lead is not first):
            self.lead_created(lead)
        for reply in (r for r in replies if first is None or r.lead_id != first.id):
            if any(lead.id == reply.lead_id for lead in leads):
                self.reply(reply)

        self.days_later(6)

    def days_later(self, days: int) -> None:
        """Simulate the scheduler firing a few days from now."""
        now = utc_now() + dt.timedelta(days=days)
        day = now.date().isoformat()
        self.header(f"{days} days later, {day}: scheduled SLA check and daily digest")
        self.handle(Event(id=f"sla:{day}", type=EventType.SLA_CHECK, payload={"now": now.isoformat()}))
        report = self.system.orchestrator.handle(
            Event(id=f"digest:{day}", type=EventType.DIGEST_DAILY, payload={"now": now.isoformat()}))
        self.out("")
        for line in report.steps:  # the digest is already formatted
            self.out(f"  {line}")

    # --- Acts --------------------------------------------------------------------

    def lead_created(self, lead: Lead) -> None:
        self.header(f"New lead: {lead.company} ({lead.contact.name}, {lead.contact.title})")
        self.handle(Event(id=f"lead:{lead.id}", type=EventType.LEAD_CREATED, payload={"lead_id": lead.id}),
                    review=False)
        deal = self.system.crm.deal_for_lead(lead.id)
        if deal.score:
            self.out("  Why this tier:")
            for name in ("fit", "intent", "engagement"):
                sub = getattr(deal.score, name)
                self.out(f"    {name} {sub.value:g}: {sub.reason}")
        self.review_approvals()
        if deal.id not in self.previewed:
            self.show_sent_email(deal.id)

    def reply(self, reply: Reply, show_duplicate: bool = False) -> None:
        self.header(f"Reply {reply.id} from {reply.sender}")
        lines = [line.strip() for line in reply.body.splitlines() if line.strip()]
        first = next((line for line in lines if not line.lower().startswith(GREETINGS)), lines[0])
        self.out(f'    "{first[:100]}{"..." if len(first) > 100 else ""}"')
        event = Event(id=f"reply:{reply.id}", type=EventType.REPLY_RECEIVED,
                      payload={"reply": reply.model_dump(mode="json", by_alias=True)})
        self.handle(event)
        if show_duplicate:
            self.out("  Same webhook delivered again (at-least-once delivery):")
            self.handle(event)

    def call(self, lead: Lead, transcript: Path) -> None:
        self.header(f"Call transcript ready (Fireflies): {transcript.name}")
        self.handle(Event(id=f"call:{transcript.stem}", type=EventType.CALL_COMPLETED,
                          payload={"lead_id": lead.id, "transcript_id": transcript.stem,
                                   "transcript": transcript.read_text()}))

    # --- Helpers -----------------------------------------------------------------

    def handle(self, event: Event, review: bool = True) -> None:
        report = self.system.orchestrator.handle(event)
        for step in report.steps:
            self.out(f"  - {step}")
        if review:
            self.review_approvals()

    def review_approvals(self) -> None:
        for request in self.system.approvals.pending():
            action = request.action
            self.out(f"\n  APPROVAL NEEDED: {action.summary}\n  Why: {request.reason}")
            if action.type is ActionType.SEND_EMAIL:
                self.show_email(action.payload)
                self.previewed.add(action.deal_id)
            elif action.type is ActionType.SEND_IO:
                self.show_io(action.payload["document"])
            approved = self.auto_approve or self.ask("  Approve? [y/N] ").strip().lower() in ("y", "yes")
            self.out(f"  {'Approved' if approved else 'Rejected'} by {REVIEWER}")
            self.handle(Event(id=f"approval:{request.id}", type=EventType.APPROVAL_DECIDED,
                              payload={"request_id": request.id, "approved": approved, "reviewer": REVIEWER}))

    def show_sent_email(self, deal_id: str) -> None:
        sent = [a for a in self.system.crm.timeline(deal_id) if a.kind == "email_sent"]
        if sent:
            self.out("  Email sent (mock outbox):")
            self.show_email(sent[-1].data)

    def show_email(self, email: dict) -> None:
        self.out(f"    To: {email['to']}\n    Subject: {email['subject']}")
        for line in email["body"].strip().splitlines():
            self.out(f"    | {line}")
        if email.get("lessons_used"):
            self.out(f"    Lessons used: {', '.join(email['lessons_used'])}")

    def show_io(self, document: str) -> None:
        for line in document.splitlines():
            if any(key in line for key in IO_SUMMARY_LINES):
                self.out(f"    {line.lstrip('- ')}")

    def header(self, title: str) -> None:
        self.act += 1
        self.out(f"\n=== {self.act}. {title} ===")

    def summary(self) -> None:
        system = self.system
        self.out("\n=== Pipeline ===")
        self.out(f"  {'Deal':<6} {'Company':<22} {'Tier':<4} {'Score':>5}  {'Stage':<11} Flags")
        for deal in sorted(system.crm.deals(), key=lambda d: d.id):
            lead = system.crm.get_lead(deal.lead_id)
            tier = deal.score.tier.value if deal.score else "-"
            score = f"{deal.score.total:g}" if deal.score else "-"
            self.out(f"  {deal.id:<6} {lead.company:<22} {tier:<4} {score:>5}  {deal.stage.value:<11} "
                     f"{', '.join(deal.flags) or '-'}")

        self.out("\n=== Knowledge loop ===")
        for lesson in system.lessons.all():
            uses = [use.deal_id for use in system.lessons.uses(lesson.id)]
            self.out(f"  - {lesson.text}\n    learned from {lesson.source} {lesson.source_id}; "
                     f"used in: {', '.join(uses) or 'not yet'}")

        events = system.audit.events()
        self.out(f"\n=== Audit log: {len(events)} entries, approvals pending: {len(system.approvals.pending())} ===")
