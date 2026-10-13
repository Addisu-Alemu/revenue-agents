"""The demo story: which events happen, in what order. Shared by the terminal demo and the UI.

The order is driven by the data, not by segment-specific code:
  1. the first lead that has a call transcript: lead -> its replies and calls in date order (a question,
     a meeting request, an intro call, a call with the CFO, then a change to the IO) -> signature
  2. every other lead in the segment, then their replies
  3. a few days later: the scheduled SLA check and daily digest
So the lesson learned on the call is available when the later emails are drafted.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from revenue_agents.events import Event, EventType
from revenue_agents.models import Lead, Reply
from revenue_agents.storage import utc_now

if TYPE_CHECKING:
    from revenue_agents.system import System

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FORGED_ENVELOPE = "ENV-FAKE-001"


@dataclass(frozen=True)
class Step:
    kind: str  # "lead", "reply", "call", "signature" or "days_later"
    title: str
    lead: Lead | None = None
    reply: Reply | None = None
    transcript: Path | None = None
    show_duplicate: bool = False
    days: int = 0


def load_replies() -> list[Reply]:
    return [Reply.model_validate(item) for item in json.loads((DATA_DIR / "replies.json").read_text(encoding="utf-8"))]


def load_transcripts() -> dict[str, list[Path]]:
    """Each lead's call transcripts (file names like L-001_intro_call.txt), in the order the calls happened."""
    calls: dict[str, list[Path]] = {}
    for path in (DATA_DIR / "transcripts").glob("*.txt"):
        calls.setdefault(path.name.split("_")[0], []).append(path)
    return {lead_id: sorted(paths, key=_call_date) for lead_id, paths in calls.items()}


def _call_date(path: Path) -> str:
    """The "Date:" line of a transcript (Fireflies exports start with it), else the file name."""
    for line in path.read_text(encoding="utf-8").splitlines()[:10]:
        if line.startswith("Date:"):
            return line.removeprefix("Date:").strip()
    return path.name


def plan(leads: list[Lead], segment: str, days_later: int = 6) -> list[Step]:
    in_segment = sorted((lead for lead in leads if lead.segment == segment), key=lambda lead: lead.id)
    replies, transcripts = load_replies(), load_transcripts()
    names = {lead.id: lead.contact.name for lead in leads}
    steps: list[Step] = []

    first = next((lead for lead in in_segment if lead.id in transcripts), None)
    if first:
        steps.append(_lead_step(first))
        calls = transcripts[first.id]
        first_call = _call_date(calls[0])
        first_replies = [r for r in replies if r.lead_id == first.id]
        # The last reply before the first call (the meeting request) is also delivered twice: at-least-once delivery.
        before_calls = [r for r in first_replies if r.received_at.date().isoformat() < first_call]
        conversation = [(r.received_at.date().isoformat(), Step(
            "reply", f"Reply {r.id} from {names.get(r.lead_id, r.sender)}", reply=r,
            show_duplicate=bool(before_calls) and r is before_calls[-1])) for r in first_replies]
        conversation += [(_call_date(path), Step("call", f"Call transcript ready (Fireflies): {path.name}", lead=first,
                                                 transcript=path)) for path in calls]
        steps += [step for _, step in sorted(conversation, key=lambda item: item[0])]  # in the order it happened
        steps.append(Step("signature", f"E-signature webhook for {first.company}'s IO", lead=first))

    steps += [_lead_step(lead) for lead in in_segment if lead is not first]
    segment_ids = {lead.id for lead in in_segment}
    steps += [Step("reply", f"Reply {r.id} from {names.get(r.lead_id, r.sender)}", reply=r)
              for r in replies if r.lead_id in segment_ids and (first is None or r.lead_id != first.id)]
    steps.append(Step("days_later", f"{days_later} days later: scheduled SLA check and daily digest", days=days_later))
    return steps


class NotReadyError(RuntimeError):
    """The step can't run yet (e.g. no IO has been sent for signature)."""


def run(system: System, step: Step) -> list[str]:
    """Run one story step through the orchestrator and return what happened, in plain words."""
    handle = system.orchestrator.handle
    if step.kind == "lead":
        return handle(lead_event(step.lead)).steps
    if step.kind == "reply":
        lines = handle(reply_event(step.reply)).steps
        if step.show_duplicate:
            lines += ["Same webhook delivered again (at-least-once delivery):"]
            lines += handle(reply_event(step.reply)).steps
        return lines
    if step.kind == "call":
        return handle(call_event(step.lead, step.transcript)).steps
    if step.kind == "signature":
        return _signature(system, step.lead)
    sla, digest = scheduled_events(step.days)
    return handle(sla).steps + [""] + handle(digest).steps


def _signature(system: System, lead: Lead) -> list[str]:
    """A forged webhook (rejected), then the customer really signs and the deal closes."""
    deal = system.crm.deal_for_lead(lead.id)
    sent = [a for a in system.crm.timeline(deal.id) if a.kind == "io_sent"]
    if not sent:
        waiting = [r for r in system.orchestrator.approved_waiting() if r.action.deal_id == deal.id]
        if waiting:
            raise NotReadyError("The IO is approved but wasn't sent (kill switch or a failure). "
                                "Use Run now on the Approvals page, or release the kill switch.")
        raise NotReadyError("No IO has been sent yet: approve it in the approvals queue first.")
    envelope_id = sent[-1].data["envelope_id"]
    signer = (deal.fields.decision_maker or lead.contact.name).split(" (")[0]

    lines = ["A forged webhook arrives first, for an envelope we never sent:"]
    lines += system.orchestrator.handle(signature_event(FORGED_ENVELOPE)).steps
    lines += [f"Then {signer} really signs in the e-sign tool, and its webhook arrives:"]
    system.esign.sign(envelope_id, signed_by=signer)  # what DocuSign would record
    return lines + system.orchestrator.handle(signature_event(envelope_id)).steps


def _lead_step(lead: Lead) -> Step:
    return Step("lead", f"New lead: {lead.company} ({lead.contact.name}, {lead.contact.title})", lead=lead)


# --- Event builders ------------------------------------------------------------------


def lead_event(lead: Lead) -> Event:
    return Event(id=f"lead:{lead.id}", type=EventType.LEAD_CREATED, payload={"lead_id": lead.id})


def reply_event(reply: Reply) -> Event:
    """The reply arrives now, as an email webhook would deliver it, so the thread reads in the order it happened."""
    arrived = reply.model_copy(update={"received_at": utc_now()})
    return Event(id=f"reply:{reply.id}", type=EventType.REPLY_RECEIVED,
                 payload={"reply": arrived.model_dump(mode="json", by_alias=True)})


def call_event(lead: Lead, transcript: Path) -> Event:
    return Event(id=f"call:{transcript.stem}", type=EventType.CALL_COMPLETED,
                 payload={"lead_id": lead.id, "transcript_id": transcript.stem, "transcript": transcript.read_text(encoding="utf-8")})


def terms_event(deal_id: str, changes: dict, by: str) -> Event:
    """A rep changed the deal's terms (e.g. Net 45 instead of Net 30). Each change is its own event."""
    return Event(id=f"terms:{deal_id}:{utc_now().isoformat()}", type=EventType.TERMS_CHANGED,
                 payload={"deal_id": deal_id, "changes": changes, "by": by})


def signature_event(envelope_id: str) -> Event:
    return Event(id=f"esign:{envelope_id}", type=EventType.SIGNATURE_COMPLETED, payload={"envelope_id": envelope_id})


def approval_event(request_id: str, approved: bool, reviewer: str, note: str | None = None,
                   save_lesson: bool = False, edits: dict[str, str] | None = None) -> Event:
    """A human's decision. `edits` (an email's subject and/or body) are the reviewer's changes before approving."""
    return Event(id=f"approval:{request_id}", type=EventType.APPROVAL_DECIDED,
                 payload={"request_id": request_id, "approved": approved, "reviewer": reviewer, "note": note,
                          "save_lesson": save_lesson, "edits": edits or {}})


def scheduled_events(days: int) -> tuple[Event, Event]:
    """The SLA check and digest the scheduler would send `days` from now."""
    now = utc_now() + dt.timedelta(days=days)
    day, payload = now.date().isoformat(), {"now": now.isoformat()}
    return (Event(id=f"sla:{day}", type=EventType.SLA_CHECK, payload=payload),
            Event(id=f"digest:{day}", type=EventType.DIGEST_DAILY, payload=payload))
