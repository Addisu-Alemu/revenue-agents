"""Audit log: an append-only record of every decision and action.

Each event is one line of JSON. Lines are only ever added, never edited, so
the log shows exactly what happened, who or what did it, and why.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from revenue_agents.models import Model
from revenue_agents.storage import STATE_DIR, append_jsonl, read_jsonl, utc_now


class AuditEvent(Model):
    at: dt.datetime
    actor: str
    event: str
    deal_id: str | None = None
    action_id: str | None = None
    detail: str = ""


class AuditLog:
    def __init__(self, path: Path = STATE_DIR / "audit.jsonl") -> None:
        self._path = Path(path)

    def record(
        self, actor: str, event: str, *, deal_id: str | None = None, action_id: str | None = None, detail: str = ""
    ) -> AuditEvent:
        entry = AuditEvent(at=utc_now(), actor=actor, event=event, deal_id=deal_id, action_id=action_id, detail=detail)
        append_jsonl(self._path, entry.model_dump(mode="json"))
        return entry

    def events(self, deal_id: str | None = None) -> list[AuditEvent]:
        entries = [AuditEvent.model_validate(record) for record in read_jsonl(self._path)]
        return [entry for entry in entries if deal_id is None or entry.deal_id == deal_id]
