"""Action claims: reserve each action id exactly once, safely across threads and processes.

Before an action runs, the gateway creates a marker file for its id with
O_CREAT | O_EXCL ("create only if it doesn't exist"). The operating system
guarantees only one caller can succeed, so two copies of the same webhook
arriving at the same moment can't both run. One file per action also means
there is no shared list that concurrent writers could overwrite.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from urllib.parse import quote

from revenue_agents.storage import STATE_DIR, read_json, utc_now, write_json


class ClaimStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ActionClaims:
    def __init__(self, directory: Path = STATE_DIR / "claims") -> None:
        self._directory = Path(directory)

    def claim(self, action_id: str) -> bool:
        """Reserve the id. Returns False if anyone (any thread or process) already has."""
        self._directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path(action_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"action_id": action_id, "status": ClaimStatus.IN_PROGRESS.value, "at": str(utc_now())}, file)
        return True

    def complete(self, action_id: str) -> None:
        write_json(self._path(action_id), {"action_id": action_id, "status": ClaimStatus.COMPLETED.value, "at": str(utc_now())})

    def release(self, action_id: str) -> None:
        """Give the id back after a failure, so a later retry can run it."""
        self._path(action_id).unlink(missing_ok=True)

    def exists(self, action_id: str) -> bool:
        return self._path(action_id).exists()

    def in_progress(self) -> list[str]:
        """Claims that never completed, e.g. after a crash mid-send. These need a human to review."""
        stuck = []
        for path in self._directory.glob("*.json"):
            record = read_json(path, {}) if path.stat().st_size else {}
            if record.get("status") != ClaimStatus.COMPLETED.value:
                stuck.append(record.get("action_id", path.stem))
        return sorted(stuck)

    def _path(self, action_id: str) -> Path:
        return self._directory / f"{quote(action_id, safe='')}.json"
