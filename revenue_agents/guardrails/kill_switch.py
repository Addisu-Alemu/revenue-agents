"""Emergency stop for every agent action.

The switch is a file: while state/KILL_SWITCH exists, the policy blocks all
actions. An operator can stop the system with `touch state/KILL_SWITCH`,
without changing code or restarting anything.
"""

from __future__ import annotations

from pathlib import Path

from revenue_agents.storage import STATE_DIR


class KillSwitch:
    def __init__(self, path: Path = STATE_DIR / "KILL_SWITCH") -> None:
        self._path = Path(path)

    def is_engaged(self) -> bool:
        return self._path.exists()

    def reason(self) -> str:
        if not self.is_engaged():
            return ""
        return self._path.read_text(encoding="utf-8").strip() or "no reason given"

    def engage(self, reason: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(reason, encoding="utf-8")

    def release(self) -> None:
        self._path.unlink(missing_ok=True)
