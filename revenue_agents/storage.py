"""Tiny file-based persistence for runtime state (approval queue, audit log).

Writes are atomic: data goes to a temporary file that then replaces the real
one in a single step, so a crash never leaves a half-written file. Locks work
on Linux, macOS (fcntl) and Windows (msvcrt). Text is always UTF-8, so files
read the same on every OS. In production this would be a database; the
interfaces would stay the same.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
ENCODING = "utf-8"

if os.name == "nt":  # pragma: no cover - runs on Windows only
    import msvcrt

    def _lock(file) -> None:
        file.seek(0)
        while True:
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)  # retries for ~10s, then raises
                return
            except OSError:
                continue  # still held by another process: keep waiting

    def _unlock(file) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

    def _try_lock(file) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)  # raises OSError at once if another process holds it

else:
    import fcntl

    def _lock(file) -> None:
        fcntl.flock(file, fcntl.LOCK_EX)

    def _unlock(file) -> None:
        fcntl.flock(file, fcntl.LOCK_UN)

    def _try_lock(file) -> None:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises OSError at once if another process holds it


# The live world runs on simulated time: while a world clock is set, every timestamp (audit, approvals, deal
# stages, stall checks) is world time, so the whole system agrees on what "now" is.
_clock: Callable[[], datetime] | None = None


def utc_now() -> datetime:
    return _clock() if _clock else datetime.now(timezone.utc)


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """Use `clock` for utc_now() in this process (None: the real time again)."""
    global _clock
    _clock = clock


def read_json(path: Path, default: Any) -> Any:
    path = Path(path)
    return json.loads(path.read_text(encoding=ENCODING)) if path.exists() else default


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name per write, so concurrent writers never share a temp file.
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding=ENCODING) as file:
        file.write(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    _replace(temp, path)


def _replace(source: str, target: Path, attempts: int = 20) -> None:
    """os.replace, retried briefly: on Windows it fails while another process has the target open."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def try_lock(path: Path):
    """Take an exclusive lock without waiting, and keep it until the returned file is closed. None if it's taken."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding=ENCODING)
    try:
        _try_lock(handle)
    except OSError:
        handle.close()
        return None
    return handle


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock for a read-modify-write of `path`, across threads and processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+", encoding=ENCODING) as lock_file:
        _lock(lock_file)
        try:
            yield
        finally:
            _unlock(lock_file)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record as a line. Existing lines are never rewritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding=ENCODING) as file:
        file.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding=ENCODING).splitlines() if line.strip()]
