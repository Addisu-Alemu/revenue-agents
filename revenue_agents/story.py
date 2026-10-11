"""The demo story: which events happen, in what order. Shared by the terminal demo and the UI.

The order is driven by the data, not by segment-specific code:
  1. the first lead that has a call transcript: lead -> its replies -> its call -> IO -> signature
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
    return [Reply.model_validate(item) for item in json.loads((DATA_DIR / "replies.json").read_text())]


def load_transcripts() -> dict[str, Path]:
    """Transcripts by lead id, from file names like L-001_discovery_call.txt."""
    return {path.name.split("_")[0]: path for path in sorted((DATA_DIR / "transcripts").glob("*.txt"))}


def plan(leads: list[Lead], segment: str, days_later: int = 6) -> list[Step]:
    in_segment = sorted((lead for lead in leads if lead.segment == segment), key=lambda lead: lead.id)
    replies, transcripts = load_replies(), load_transcripts()
    names = {lead.id: lead.contact.name for lead in leads}
    steps: list[Step] = []

    first = next((lead for lead in in_segment if lead.id in transcripts), None)
    if first:
        steps.append(_lead_step(first))
        steps += [Step("reply", f"Reply {r.id} from {names.get(r.lead_id, r.sender)}", reply=r, show_duplicate=True)
                  for r in replies if r.lead_id == first.id]
        steps.append(Step("call", f"Call transcript ready (Fireflies): {transcripts[first.id].name}", lead=first,
                          transcript=transcripts[first.id]))
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
    return Event(id=f"reply:{reply.id}", type=EventType.REPLY_RECEIVED,
                 payload={"reply": reply.model_dump(mode="json", by_alias=True)})


def call_event(lead: Lead, transcript: Path) -> Event:
    return Event(id=f"call:{transcript.stem}", type=EventType.CALL_COMPLETED,
                 payload={"lead_id": lead.id, "transcript_id": transcript.stem, "transcript": transcript.read_text()})


def signature_event(envelope_id: str) -> Event:
    return Event(id=f"esign:{envelope_id}", type=EventType.SIGNATURE_COMPLETED, payload={"envelope_id": envelope_id})


def approval_event(request_id: str, approved: bool, reviewer: str, note: str | None = None,
                   save_lesson: bool = False) -> Event:
    return Event(id=f"approval:{request_id}", type=EventType.APPROVAL_DECIDED,
                 payload={"request_id": request_id, "approved": approved, "reviewer": reviewer, "note": note,
                          "save_lesson": save_lesson})


def scheduled_events(days: int) -> tuple[Event, Event]:
    """The SLA check and digest the scheduler would send `days` from now."""
    now = utc_now() + dt.timedelta(days=days)
    day, payload = now.date().isoformat(), {"now": now.isoformat()}
    return (Event(id=f"sla:{day}", type=EventType.SLA_CHECK, payload=payload),
            Event(id=f"digest:{day}", type=EventType.DIGEST_DAILY, payload=payload))
