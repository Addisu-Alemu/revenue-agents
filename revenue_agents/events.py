"""Events: the triggers that start work (webhooks, schedules, human decisions).

Each event carries an id from its source (e.g. the reply id or the webhook
delivery id). The orchestrator claims that id before processing, so an event
delivered twice is processed once.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import Field

from revenue_agents.models import Model
from revenue_agents.storage import utc_now


class EventType(str, Enum):
    LEAD_CREATED = "lead.created"  # HubSpot webhook: new contact / form fill
    REPLY_RECEIVED = "reply.received"  # email provider webhook
    CALL_COMPLETED = "call.completed"  # Fireflies webhook: transcript ready
    APPROVAL_DECIDED = "approval.decided"  # a human approved or rejected in the queue
    SIGNATURE_COMPLETED = "signature.completed"  # e-sign webhook (DocuSign): an envelope was signed
    TERMS_CHANGED = "terms.changed"  # a rep changed the deal's commercial terms (UI): the IO is redrafted
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
