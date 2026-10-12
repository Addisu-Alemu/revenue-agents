"""Lesson store: the knowledge loop's memory.

Lessons come from calls and replies (each one records its source). Agents ask
for the lessons relevant to a segment and purpose: that segment's own lessons
plus global ones learned in any segment. Every time a lesson
shapes an email, that use is recorded. So a lesson can be traced from the
call where it was learned to the emails it influenced.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from revenue_agents.models import Lesson, Model
from revenue_agents.storage import STATE_DIR, locked, read_json, utc_now, write_json


class LessonUse(Model):
    lesson_id: str
    used_in: str  # the action that used it, e.g. "send_email:D-002:first_touch"
    deal_id: str
    at: dt.datetime


class LessonStore:
    def __init__(self, path: Path = STATE_DIR / "lessons.json") -> None:
        self._path = Path(path)

    def add(self, lesson: Lesson) -> bool:
        """Save a lesson. Returns False if it was already saved (safe to retry)."""
        with locked(self._path):
            state = self._read()
            if lesson.id in state["lessons"]:
                return False
            state["lessons"][lesson.id] = lesson.model_dump(mode="json")
            write_json(self._path, state)
            return True

    def all(self) -> list[Lesson]:
        return [Lesson.model_validate(item) for item in self._read()["lessons"].values()]

    def for_segment(self, segment: str, purpose: str = "outreach", limit: int = 5) -> list[Lesson]:
        """The newest lessons for this segment and purpose, including global lessons learned in any segment."""
        matching = [lesson for lesson in self.all() if lesson.applies_in(segment) and purpose in lesson.applies_to]
        return sorted(matching, key=lambda lesson: lesson.created_at, reverse=True)[:limit]

    def record_use(self, lesson_ids: list[str], used_in: str, deal_id: str) -> None:
        if not lesson_ids:
            return
        with locked(self._path):
            state = self._read()
            state["uses"] += [
                LessonUse(lesson_id=lesson_id, used_in=used_in, deal_id=deal_id, at=utc_now()).model_dump(mode="json")
                for lesson_id in lesson_ids
            ]
            write_json(self._path, state)

    def uses(self, lesson_id: str | None = None) -> list[LessonUse]:
        records = [LessonUse.model_validate(item) for item in self._read()["uses"]]
        return [use for use in records if lesson_id is None or use.lesson_id == lesson_id]

    def _read(self) -> dict[str, Any]:
        return read_json(self._path, {"lessons": {}, "uses": []})
