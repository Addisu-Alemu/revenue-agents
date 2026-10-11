"""CRM: the system of record (HubSpot in production, mocked here).

Everything that is true about a lead or deal lives here: the deal's stage,
score and fields, and a timeline of everything that happened to it. Agents
and the orchestrator depend on the CRM interface, so swapping MockHubSpot for
a real HubSpot client is one new class and no other changes.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from revenue_agents.models import Deal, Lead, Message, Model, Stage
from revenue_agents.storage import STATE_DIR, locked, read_json, utc_now, write_json

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class Activity(Model):
    """One entry on a deal's timeline, like a HubSpot engagement."""

    id: str
    deal_id: str
    at: dt.datetime
    kind: str  # e.g. "email_sent", "reply", "document", "flag", "note", "stage_change"
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class CRM(Protocol):
    def leads(self) -> list[Lead]: ...
    def get_lead(self, lead_id: str) -> Lead: ...
    def add_lead(self, lead: Lead) -> None: ...
    def deals(self) -> list[Deal]: ...
    def get_deal(self, deal_id: str) -> Deal: ...
    def deal_for_lead(self, lead_id: str) -> Deal: ...
    def save_deal(self, deal: Deal) -> None: ...
    def log(self, deal_id: str, kind: str, summary: str, data: dict[str, Any] | None = None) -> Activity: ...
    def timeline(self, deal_id: str) -> list[Activity]: ...
    def add_message(self, message: Message) -> None: ...
    def messages(self, deal_id: str) -> list[Message]: ...


class MockHubSpot:
    """A JSON-file CRM, seeded with the mock leads on first use. Every change is locked and atomic."""

    def __init__(self, path: Path = STATE_DIR / "hubspot.json", seed_leads: Path = DATA_DIR / "leads.json") -> None:
        self._path = Path(path)
        self._seed_leads = Path(seed_leads)

    # --- Leads -----------------------------------------------------------------

    def leads(self) -> list[Lead]:
        return [Lead.model_validate(item) for item in self._read()["leads"].values()]

    def get_lead(self, lead_id: str) -> Lead:
        return Lead.model_validate(self._get(self._read()["leads"], lead_id, "lead"))

    def add_lead(self, lead: Lead) -> None:
        with locked(self._path):
            state = self._read()
            state["leads"].setdefault(lead.id, lead.model_dump(mode="json"))
            write_json(self._path, state)

    # --- Deals -----------------------------------------------------------------

    def deals(self) -> list[Deal]:
        return [Deal.model_validate(item) for item in self._read()["deals"].values()]

    def get_deal(self, deal_id: str) -> Deal:
        return Deal.model_validate(self._get(self._read()["deals"], deal_id, "deal"))

    def deal_for_lead(self, lead_id: str) -> Deal:
        """The lead's deal, created at the prospect stage the first time. Safe to call repeatedly."""
        with locked(self._path):
            state = self._read()
            lead = Lead.model_validate(self._get(state["leads"], lead_id, "lead"))
            deal_id = "D-" + lead_id.removeprefix("L-")
            if deal_id not in state["deals"]:
                deal = Deal(id=deal_id, lead_id=lead.id, segment=lead.segment, stage=Stage.PROSPECT,
                            stage_entered_at=utc_now())
                state["deals"][deal_id] = deal.model_dump(mode="json")
                write_json(self._path, state)
            return Deal.model_validate(state["deals"][deal_id])

    def save_deal(self, deal: Deal) -> None:
        with locked(self._path):
            state = self._read()
            self._get(state["deals"], deal.id, "deal")
            state["deals"][deal.id] = deal.model_dump(mode="json")
            write_json(self._path, state)

    # --- Timeline --------------------------------------------------------------

    def log(self, deal_id: str, kind: str, summary: str, data: dict[str, Any] | None = None) -> Activity:
        with locked(self._path):
            state = self._read()
            activity = Activity(id=f"A-{len(state['activities']) + 1}", deal_id=deal_id, at=utc_now(),
                                kind=kind, summary=summary, data=data or {})
            state["activities"].append(activity.model_dump(mode="json"))
            write_json(self._path, state)
            return activity

    def timeline(self, deal_id: str) -> list[Activity]:
        return [Activity.model_validate(a) for a in self._read()["activities"] if a["deal_id"] == deal_id]

    # --- Conversation ------------------------------------------------------------

    def add_message(self, message: Message) -> None:
        """Store an email in the deal's conversation. The same message id is stored once."""
        with locked(self._path):
            state = self._read()
            if not any(item["id"] == message.id for item in state["messages"]):
                state["messages"].append(message.model_dump(mode="json"))
                write_json(self._path, state)

    def messages(self, deal_id: str) -> list[Message]:
        return [Message.model_validate(m) for m in self._read()["messages"] if m["deal_id"] == deal_id]

    # --- Internals -------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        state = read_json(self._path, None)
        if state is None:
            leads = json.loads(self._seed_leads.read_text(encoding="utf-8"))
            state = {"leads": {lead["id"]: lead for lead in leads}, "deals": {}, "activities": []}
        state.setdefault("messages", [])
        return state

    @staticmethod
    def _get(records: dict[str, Any], key: str, kind: str) -> Any:
        if key not in records:
            raise KeyError(f"no {kind} '{key}'")
        return records[key]
