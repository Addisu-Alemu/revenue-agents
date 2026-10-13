"""The live demo in the terminal: one segment's story, run through the real system.

The order of events comes from story.py (shared with the UI). High-risk actions
pause for a human (or are approved automatically with --auto-approve). Scripted
mode is always announced as scripted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from revenue_agents import story
from revenue_agents.events import Event
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.llm import LLMError
from revenue_agents.models import Lead, Reply
from revenue_agents.segments import SegmentConfigError, SegmentNotFoundError
from revenue_agents.storage import STATE_DIR
from revenue_agents.system import System, build_system, make_llm, reset_state

DEMO_STATE = STATE_DIR / "demo"
REVIEWER = "demo-reviewer@cool.example"
IO_SUMMARY_LINES = ("# Insertion Order", "| Signatory", "**Flight dates:**", "**Total budget:**", "**Payment terms:**")
GREETINGS = ("hi ", "hello", "dear ", "hey ")

Out = Callable[[str], None]
Ask = Callable[[str], str]


def run(segment: str = "brand", llm_mode: str = "scripted", state_dir: Path = DEMO_STATE, auto_approve: bool = False,
        fresh: bool = True, out: Out = print, ask: Ask = input) -> int:
    state_dir = Path(state_dir)
    if fresh:
        reset_state(state_dir)  # keeps the kill switch: a fresh run never silently turns it off

    out(f"Revenue Agents demo | segment: {segment} | model: {llm_mode}")
    if llm_mode == "scripted":
        out("NOTE: scripted mode. Model answers are scripted for this mock data; everything else is the real system.")
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
        for step in story.plan(self.system.crm.leads(), segment):
            self.header(step.title)
            if step.kind == "lead":
                self.lead_created(step.lead)
            elif step.kind == "reply":
                self.reply(step.reply, step.show_duplicate)
            elif step.kind == "call":
                self.call(step.lead, step.transcript)
            elif step.kind == "signature":
                self.signature(step)
            else:
                self.days_later(step.days)

    def days_later(self, days: int) -> None:
        """Simulate the scheduler firing a few days from now."""
        sla, digest = story.scheduled_events(days)
        self.handle(sla)
        report = self.system.orchestrator.handle(digest)
        self.out("")
        for line in report.steps:  # the digest is already formatted
            self.out(f"  {line}")

    # --- Acts --------------------------------------------------------------------

    def lead_created(self, lead: Lead) -> None:
        self.handle(story.lead_event(lead), review=False)
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
        lines = [line.strip() for line in reply.body.splitlines() if line.strip()]
        first = next((line for line in lines if not line.lower().startswith(GREETINGS)), lines[0])
        self.out(f'    "{first[:100]}{"..." if len(first) > 100 else ""}"')
        event = story.reply_event(reply)
        deal = next((d for d in self.system.crm.deals() if d.lead_id == reply.lead_id), None)
        before = len([a for a in self.system.crm.timeline(deal.id) if a.kind == "call_brief"]) if deal else 0
        self.handle(event)
        deal = next((d for d in self.system.crm.deals() if d.lead_id == reply.lead_id), None)
        briefs = [a for a in self.system.crm.timeline(deal.id) if a.kind == "call_brief"] if deal else []
        if len(briefs) > before:  # only a brief this reply produced
            self.out("  Call brief for the rep (before the meeting):")
            for line in briefs[-1].data["markdown"].splitlines():
                if line.strip():
                    self.out(f"    {line.replace('**', '').replace('### ', '')}")
        if show_duplicate:
            self.out("  Same webhook delivered again (at-least-once delivery):")
            self.handle(event)

    def call(self, lead: Lead, transcript: Path) -> None:
        self.handle(story.call_event(lead, transcript))

    def signature(self, step: story.Step) -> None:
        try:
            lines = story.run(self.system, step)
        except story.NotReadyError:
            self.out("  No IO was sent, so there is nothing to sign.")
            return
        for line in lines:
            self.out(f"  - {line}")
        deal = self.system.crm.deal_for_lead(step.lead.id)
        handoff = [a for a in self.system.crm.timeline(deal.id) if a.kind == "handoff"]
        if handoff:
            data = handoff[-1].data
            self.out(f"  Handoff to delivery: {', '.join(data['products'])}; ${data['budget_usd']:,}; "
                     f"{data['flight_start']} to {data['flight_end']}")

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
            note, save_lesson = None, False
            if not approved and action.type is ActionType.SEND_EMAIL:
                note = self.ask("  Feedback for a redraft (empty = just reject): ").strip() or None
                if note:
                    save_lesson = self.ask("  Save it as a lesson for future emails? [y/N] ").strip().lower() in (
                        "y", "yes")
            self.out(f"  {'Approved' if approved else 'Rejected'} by {REVIEWER}")
            self.handle(story.approval_event(request.id, approved, REVIEWER, note, save_lesson))

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
            uses = [f"{use.deal_id} ({'call brief' if 'call_brief' in use.used_in else 'email'})"
                    for use in system.lessons.uses(lesson.id)]
            self.out(f"  - {lesson.text}\n    learned from {lesson.source} {lesson.source_id}; "
                     f"used in: {', '.join(uses) or 'not yet'}")

        events = system.audit.events()
        self.out(f"\n=== Audit log: {len(events)} entries, approvals pending: {len(system.approvals.pending())} ===")
