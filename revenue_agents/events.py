"""Events: the triggers that start work (webhooks, schedules, human decisions).

Each event carries an id from its source (e.g. the reply id or the webhook
delivery id). The orchestrator claims that id before processing, so an event
delivered twice is processed once.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field

from revenue_agents.models import Lead, Model, Reply
from revenue_agents.storage import utc_now


class EventType(str, Enum):
    LEAD_CREATED = "lead.created"  # HubSpot webhook: new contact / form fill
    REPLY_RECEIVED = "reply.received"  # email provider webhook
    CALL_COMPLETED = "call.completed"  # Fireflies webhook: transcript ready
    APPROVAL_DECIDED = "approval.decided"  # a human approved or rejected in the queue
    SIGNATURE_COMPLETED = "signature.completed"  # e-sign webhook (DocuSign): an envelope was signed
    TERMS_CHANGED = "terms.changed"  # a rep changed the deal's commercial terms (UI): the IO is redrafted
    FOLLOW_UP_DUE = "follow_up.due"  # scheduler: a follow-up an agent set for a date is due
    SLA_CHECK = "sla.check"  # scheduler (e.g. every morning): find stalled deals, send reminders
    DIGEST_DAILY = "digest.daily"  # scheduler (daily): summary for the sales team


class Event(Model):
    id: str  # scheduled events use the date, e.g. "digest:2026-10-10", so each day runs once
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    received_at: dt.datetime = Field(default_factory=utc_now)

    @property
    def now(self) -> dt.datetime:
        """The time the event is about: an explicit payload "now" (scheduler, tests), else when it arrived."""
        value = self.payload.get("now")
        return dt.datetime.fromisoformat(value) if value else self.received_at


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


def follow_up_event(deal_id: str, due: str, reason: str) -> Event:
    """A follow-up an agent scheduled is due. One per deal and date, so a redelivery is ignored."""
    return Event(id=f"follow_up:{deal_id}:{due}", type=EventType.FOLLOW_UP_DUE,
                 payload={"deal_id": deal_id, "due": due, "reason": reason})


def sla_event(now: dt.datetime) -> Event:
    return Event(id=f"sla:{now.date().isoformat()}", type=EventType.SLA_CHECK, payload={"now": now.isoformat()})


def digest_event(now: dt.datetime) -> Event:
    return Event(id=f"digest:{now.date().isoformat()}", type=EventType.DIGEST_DAILY, payload={"now": now.isoformat()})


def scheduled_events(days: int) -> tuple[Event, Event]:
    """The SLA check and digest the scheduler would send `days` from now."""
    now = utc_now() + dt.timedelta(days=days)
    return sla_event(now), digest_event(now)
