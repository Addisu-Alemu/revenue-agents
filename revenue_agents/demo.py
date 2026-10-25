"""The live demo in the terminal: the world runs at full speed through the real system.

Leads arrive when they were created, prospects answer what the agents actually
sent, calls and signatures happen when they would, and the scheduler runs every
morning and evening (world.py). Whenever an action waits for a human, the demo
asks you, or approves it with --auto-approve. Scripted mode is always announced
as scripted.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

from revenue_agents.events import approval_event
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.segments import SegmentConfigError, SegmentNotFoundError
from revenue_agents.storage import STATE_DIR, set_clock
from revenue_agents.system import System, reset_state
from revenue_agents.world import Delivered, Desk

DEMO_STATE = STATE_DIR / "demo"
REVIEWER = "demo-reviewer@cool.example"
IO_SUMMARY_LINES = ("# Insertion Order", "| Signatory", "**Flight dates:**", "**Total budget:**", "**Payment terms:**")
DAYS = 14  # how much simulated time the demo runs: the brand deal closes on day 12

Out = Callable[[str], None]
Ask = Callable[[str], str]


def run(segment: str | None = None, llm_mode: str = "scripted", state_dir: Path = DEMO_STATE,
        auto_approve: bool = False, fresh: bool = True, out: Out = print, ask: Ask = input, days: int = DAYS) -> int:
    state_dir = Path(state_dir)
    if fresh:
        reset_state(state_dir)  # keeps the kill switch: a fresh run never silently turns it off

    out(f"Revenue Agents demo | segment: {segment or 'all'} | model: {llm_mode}")
    if llm_mode == "scripted":
        out("NOTE: scripted mode. Agent decisions are scripted for this mock data; every tool, rule and side effect "
            "is the real system.")
    desk = Desk(state_dir)
    try:
        if segment:
            out(f"Segment pack: {desk.system().segments.get(segment).display_name}")
        if fresh or not desk.world.exists():
            desk.start(model=llm_mode)
        set_clock(desk.world.now)
        demo = _Demo(desk, out, ask, auto_approve, segment)
        start = desk.world.now()
        out(f"World clock starts {start:%a %d %b %H:%M} UTC and runs {days} days.")
        desk.run_until(start + dt.timedelta(days=days), on_delivered=demo.show, between=demo.review_approvals)
        error = desk.world.read().error
        if error:
            out(f"FAILED: {error}")
            return 1
        demo.summary()
        return 0
    except (SegmentNotFoundError, SegmentConfigError) as error:
        out(f"Cannot run segment '{segment}': {error}")
        return 1
    except RuntimeError as error:  # e.g. the UI's runner already delivers this world
        out(f"Cannot run: {error}. Stop the UI (or run_world.py), or use another state folder.")
        return 1
    finally:
        set_clock(None)


class _Demo:
    def __init__(self, desk: Desk, out: Out, ask: Ask, auto_approve: bool, segment: str | None) -> None:
        self.desk, self.out, self.ask, self.auto_approve, self.segment = desk, out, ask, auto_approve, segment
        self.asked: set[str] = set()
        self.briefs: dict[str, int] = {}

    @property
    def system(self) -> System:
        return self.desk.system()

    def show(self, delivered: Delivered) -> None:
        if not self._mine(delivered) or self._quiet(delivered):
            return
        self.out(f"\n=== {delivered.at:%a %d %b %H:%M} · {delivered.label} ===")
        for step in delivered.steps:
            self.out(f"  - {step}")
        if delivered.source == "lead":
            for deal in self.system.crm.deals():
                if delivered.label == f"New lead: {self.system.crm.get_lead(deal.lead_id).company}" and deal.score:
                    self._why_this_tier(deal)
        self._new_call_briefs()
        self._handoffs(delivered)

    def review_approvals(self) -> None:
        """Before the world moves on: ask about every action that started waiting for a human."""
        for request in self.system.approvals.pending():
            if request.id in self.asked:
                continue
            self.asked.add(request.id)
            action = request.action
            self.out(f"\n  APPROVAL NEEDED: {action.summary}\n  Why: {request.reason}")
            if action.type is ActionType.SEND_EMAIL:
                self.show_email(action.payload)
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
            label = f"{'Approved' if approved else 'Rejected'} by you: {action.summary}"
            self.desk.world.submit(approval_event(request.id, approved, REVIEWER, note, save_lesson), label)

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

    def summary(self) -> None:
        system = self.system
        digests = [d for d in self.desk.world.log() if d.label == "Daily digest"]
        if digests:
            self.out(f"\n=== The last daily digest ({digests[-1].at:%a %d %b}) ===")
            for line in digests[-1].steps:
                self.out(f"  {line}")

        self.out("\n=== Pipeline ===")
        self.out(f"  {'Deal':<6} {'Company':<25} {'Tier':<4} {'Score':>5}  {'Stage':<11} Flags")
        for deal in sorted(system.crm.deals(), key=lambda d: d.id):
            lead = system.crm.get_lead(deal.lead_id)
            tier = deal.score.tier.value if deal.score else "-"
            score = f"{deal.score.total:g}" if deal.score else "-"
            self.out(f"  {deal.id:<6} {lead.company:<25} {tier:<4} {score:>5}  {deal.stage.value:<11} "
                     f"{', '.join(deal.flags) or '-'}")

        self.out("\n=== Coming up (timers the agents set) ===")
        for item in self.desk.world.upcoming(("timer",)):
            self.out(f"  {item.at:%a %d %b %Y}: {item.label}")

        self.out("\n=== Knowledge loop ===")
        for lesson in system.lessons.all():
            uses = [f"{use.deal_id} ({'call brief' if 'call_brief' in use.used_in else 'email'})"
                    for use in system.lessons.uses(lesson.id)]
            self.out(f"  - {lesson.text}\n    learned from {lesson.source} {lesson.source_id}; "
                     f"used in: {', '.join(uses) or 'not yet'}")

        events = system.audit.events()
        self.out(f"\n=== Audit log: {len(events)} entries, approvals pending: {len(system.approvals.pending())} ===")

    # --- Helpers -----------------------------------------------------------------------------

    def _mine(self, delivered: Delivered) -> bool:
        if not self.segment or delivered.source == "scheduler":
            return True
        companies = [self.system.crm.get_lead(deal.lead_id).company for deal in self.system.crm.deals()
                     if deal.segment != self.segment]
        text = " ".join([delivered.label, *delivered.steps])
        return not any(company in text for company in companies)

    def _quiet(self, delivered: Delivered) -> bool:
        """Scheduler runs that found nothing, and every digest (the last one is shown at the end)."""
        return delivered.label == "Daily digest" or delivered.steps == ["SLA check: 0 stalled deal(s)"]

    def _why_this_tier(self, deal) -> None:
        self.out("  Why this tier:")
        for name in ("fit", "intent", "engagement"):
            sub = getattr(deal.score, name)
            self.out(f"    {name} {sub.value:g}: {sub.reason}")

    def _new_call_briefs(self) -> None:
        for deal in self.system.crm.deals():
            briefs = [a for a in self.system.crm.timeline(deal.id) if a.kind == "call_brief"]
            if len(briefs) > self.briefs.get(deal.id, 0):
                self.briefs[deal.id] = len(briefs)
                self.out("  Call brief for the rep:")
                for line in briefs[-1].data["markdown"].splitlines():
                    if line.strip():
                        self.out(f"    {line.replace('**', '').replace('### ', '')}")

    def _handoffs(self, delivered: Delivered) -> None:
        if not any("handoff to delivery" in step for step in delivered.steps):
            return
        for deal in self.system.crm.deals():
            handoff = [a for a in self.system.crm.timeline(deal.id) if a.kind == "handoff"]
            if handoff:
                data = handoff[-1].data
                self.out(f"  Handoff to delivery: {', '.join(data['products'])}; ${data['budget_usd']:,}; "
                         f"{data['flight_start']} to {data['flight_end']}")
