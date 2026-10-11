"""Events held while the kill switch is on, kept in arrival order and replayed on release.

The kill switch is a pause, not a delete: nothing runs while it is on, and
nothing that arrived in the meantime is lost.
"""

from __future__ import annotations

from pathlib import Path

from revenue_agents.events import Event
from revenue_agents.storage import STATE_DIR, locked, read_json, write_json


class HeldEvents:
    def __init__(self, path: Path = STATE_DIR / "held_events.json") -> None:
        self._path = Path(path)

    def hold(self, event: Event) -> bool:
        """Keep the event for later. Returns False if it is already held (e.g. a redelivered webhook)."""
        with locked(self._path):
            events = read_json(self._path, [])
            if any(item["id"] == event.id for item in events):
                return False
            events.append(event.model_dump(mode="json"))
            write_json(self._path, events)
            return True

    def all(self) -> list[Event]:
        return [Event.model_validate(item) for item in read_json(self._path, [])]

    def remove(self, event_id: str) -> None:
        with locked(self._path):
            write_json(self._path, [item for item in read_json(self._path, []) if item["id"] != event_id])
