"""Tiny file-based persistence for runtime state (approval queue, audit log).

Writes are atomic: data goes to a temporary file that then replaces the real
one in a single step, so a crash never leaves a half-written file. In
production this would be a database; the interfaces would stay the same.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default: Any) -> Any:
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(temp, path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record as a line. Existing lines are never rewritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(record, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
