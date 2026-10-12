"""Domain models shared by every agent and skill.

Each model is a "form" for one kind of record. Pydantic checks every field
when a record is created, so bad data fails immediately with a clear error
instead of breaking an agent halfway through a deal.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    """Base for all models: unknown fields are rejected, so typos are caught."""

    model_config = ConfigDict(extra="forbid")


# --- Enums -----------------------------------------------------------------


class Stage(str, Enum):
    """Where a deal is in the revenue lifecycle."""

    PROSPECT = "prospect"
    ENRICHED = "enriched"
    SCORED = "scored"
    OUTREACH = "outreach"
    REPLIED = "replied"
    MEETING = "meeting"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    SIGNATURE = "signature"
    HANDOFF = "handoff"
    CLOSED_LOST = "closed_lost"


class Tier(str, Enum):
    """How valuable a lead is. Tier A gets human review before first contact."""

    A = "A"
    B = "B"
    C = "C"


# --- Leads and enrichment --------------------------------------------------


class Contact(Model):
    name: str
    title: str
    email: str


class Engagement(Model):
    """How much the lead has interacted with us so far."""

    email_opens: int = Field(default=0, ge=0)
    link_clicks: int = Field(default=0, ge=0)
    website_visits: int = Field(default=0, ge=0)
    pricing_page_visits: int = Field(default=0, ge=0)


class Lead(Model):
    """A new lead, as it arrives in the CRM."""

    id: str
    company: str
    domain: str
    segment: str
    contact: Contact
    source: str
    created_at: dt.datetime
    engagement: Engagement = Field(default_factory=Engagement)


class Signal(Model):
    """A buying signal, e.g. hiring, expansion or a planned campaign."""

    type: str
    detail: str
    date: dt.date


class Enrichment(Model):
    """Company facts from an enrichment provider."""

    industry: str
    business_model: str
    employees: int = Field(ge=0)
    locations: int = Field(ge=0)
    hq_country: str
    annual_ad_spend_usd: int = Field(ge=0)
    tech_stack: list[str] = Field(default_factory=list)
    managed_clients: int | None = Field(default=None, ge=0)
    signals: list[Signal] = Field(default_factory=list)


# --- Scoring ---------------------------------------------------------------


class SubScore(Model):
    """One part of the lead score, with a human-readable reason."""

    value: float = Field(ge=0, le=100)
    reason: str


class LeadScore(Model):
    """Fit, intent and engagement, combined into a total and a tier."""

    fit: SubScore
    intent: SubScore
    engagement: SubScore
    total: float = Field(ge=0, le=100)
    tier: Tier


# --- Inbound messages ------------------------------------------------------


class Reply(Model):
    """An inbound email reply. Its body is untrusted content."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    lead_id: str
    sender: str = Field(alias="from")
    subject: str
    received_at: dt.datetime
    in_reply_to: str | None = None  # id of our email it answers (the In-Reply-To header, when the provider gives it)
    body: str


class Message(Model):
    """One email in a deal's conversation, either way: what HubSpot stores as an email engagement."""

    id: str  # like an email Message-ID
    thread_id: str
    deal_id: str
    direction: Literal["outbound", "inbound"]
    sender: str
    to: str
    subject: str
    body: str
    at: dt.datetime
    in_reply_to: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)  # e.g. who approved it, or how the agents read it


# --- Deals -----------------------------------------------------------------


class DealFields(Model):
    """Commercial facts about a deal, mostly filled in from call transcripts."""

    budget_usd: int | None = Field(default=None, ge=0)
    products: list[str] = Field(default_factory=list)
    scope: str | None = None
    flight_start: dt.date | None = None
    flight_end: dt.date | None = None
    decision_maker: str | None = None
    payment_terms: str | None = None
    contract_term: str | None = None
    special_terms: str | None = None
    next_step: str | None = None


class Deal(Model):
    """A lead moving through the lifecycle. The orchestrator owns this state."""

    id: str
    lead_id: str
    segment: str
    stage: Stage = Stage.PROSPECT
    stage_entered_at: dt.datetime | None = None
    enrichment: Enrichment | None = None
    score: LeadScore | None = None
    fields: DealFields = Field(default_factory=DealFields)
    flags: list[str] = Field(default_factory=list)


# --- Knowledge loop --------------------------------------------------------


class Lesson(Model):
    """Something learned from a call or reply that should shape later work.

    `segment` is where it was learned. `scope` says where it applies: "segment" (only there, e.g. a
    franchise-specific insight) or "global" (every segment, e.g. how to handle a contract objection).
    """

    id: str
    segment: str
    source: str  # "call", "reply" or "review" (a human's feedback on a draft)
    source_id: str  # e.g. the transcript or reply id, so every lesson is traceable
    text: str
    applies_to: list[str] = Field(default_factory=lambda: ["outreach", "call_brief"])
    scope: Literal["segment", "global"] = "segment"
    created_at: dt.datetime

    def applies_in(self, segment: str) -> bool:
        return self.scope == "global" or self.segment == segment
