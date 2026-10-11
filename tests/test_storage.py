"""Storage works the same on every OS: UTF-8 text, atomic writes that survive a briefly locked file."""

import os

import pytest

from revenue_agents import storage
from revenue_agents.guardrails.kill_switch import KillSwitch


def test_text_is_utf8_on_every_os(tmp_path):
    storage.write_json(tmp_path / "data.json", {"name": "Café Rénové", "note": "Pilot · Q1 — 2027"})
    assert (tmp_path / "data.json").read_text(encoding="utf-8").count("Café Rénové") == 1
    assert storage.read_json(tmp_path / "data.json", {})["note"] == "Pilot · Q1 — 2027"


def test_kill_switch_reason_keeps_accents(tmp_path):
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    switch.engage("Problème avec les e-mails")
    assert switch.reason() == "Problème avec les e-mails"


def test_replace_retries_while_the_file_is_briefly_locked(tmp_path, monkeypatch):
    """On Windows, replacing a file fails while another process has it open. We retry briefly."""
    real_replace, failures = os.replace, []

    def flaky_replace(source, target):
        if len(failures) < 2:
            failures.append(1)
            raise PermissionError("file in use")
        real_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", flaky_replace)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    storage.write_json(tmp_path / "data.json", {"ok": True})
    assert storage.read_json(tmp_path / "data.json", {}) == {"ok": True}


def test_replace_gives_up_after_too_many_attempts(tmp_path, monkeypatch):
    def always_locked(source, target):
        raise PermissionError("file in use")

    monkeypatch.setattr(storage.os, "replace", always_locked)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    with pytest.raises(PermissionError):
        storage.write_json(tmp_path / "data.json", {"ok": True})
