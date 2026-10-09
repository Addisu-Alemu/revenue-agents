"""The approval queue and audit log persist to disk and can't be misused."""

import pytest

from revenue_agents.guardrails.approvals import ApprovalQueue, ApprovalStatus
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import Action, ActionType


@pytest.fixture
def queue(tmp_path):
    return ApprovalQueue(tmp_path / "approvals.json")


@pytest.fixture
def send_io():
    return Action(id="send_io:D-001", type=ActionType.SEND_IO, agent="deal_desk", deal_id="D-001", summary="Send IO")


# --- Approval queue ----------------------------------------------------------


def test_submitted_action_is_pending(queue, send_io):
    request = queue.submit(send_io, "IOs need approval")
    assert request.status is ApprovalStatus.PENDING
    assert [r.id for r in queue.pending()] == ["send_io:D-001"]


def test_resubmitting_same_action_does_not_duplicate(queue, send_io):
    queue.submit(send_io, "first")
    queue.submit(send_io, "retry")
    assert len(queue.pending()) == 1


def test_approve_records_who_and_when(queue, send_io):
    queue.submit(send_io, "IOs need approval")
    request = queue.approve("send_io:D-001", reviewer="maria@cool.ai", note="Terms checked")
    assert request.status is ApprovalStatus.APPROVED
    assert request.decided_by == "maria@cool.ai"
    assert request.decided_at is not None
    assert queue.pending() == []


def test_cannot_decide_twice(queue, send_io):
    queue.submit(send_io, "IOs need approval")
    queue.reject("send_io:D-001", reviewer="maria@cool.ai", note="Wrong budget")
    with pytest.raises(ValueError, match="already rejected"):
        queue.approve("send_io:D-001", reviewer="someone@cool.ai")


def test_unknown_request_raises(queue):
    with pytest.raises(KeyError):
        queue.get("nope")
    with pytest.raises(KeyError):
        queue.approve("nope", reviewer="maria@cool.ai")


def test_queue_survives_restart(tmp_path, send_io):
    ApprovalQueue(tmp_path / "approvals.json").submit(send_io, "IOs need approval")
    assert ApprovalQueue(tmp_path / "approvals.json").get("send_io:D-001").reason == "IOs need approval"


# --- Audit log ---------------------------------------------------------------


def test_new_audit_log_is_empty(tmp_path):
    assert AuditLog(tmp_path / "audit.jsonl").events() == []


def test_audit_log_records_and_filters_by_deal(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("research", "executed", deal_id="D-001", detail="Enriched")
    log.record("research", "executed", deal_id="D-002", detail="Enriched")

    assert len(log.events()) == 2
    assert [event.deal_id for event in log.events(deal_id="D-001")] == ["D-001"]


def test_audit_log_is_append_only_across_restarts(tmp_path):
    AuditLog(tmp_path / "audit.jsonl").record("a", "first")
    AuditLog(tmp_path / "audit.jsonl").record("b", "second")
    assert [event.event for event in AuditLog(tmp_path / "audit.jsonl").events()] == ["first", "second"]


# --- Kill switch ---------------------------------------------------------------


def test_kill_switch_engage_and_release(tmp_path):
    switch = KillSwitch(tmp_path / "KILL_SWITCH")
    assert not switch.is_engaged()
    assert switch.reason() == ""

    switch.engage("Investigating bad emails")
    assert switch.is_engaged()
    assert switch.reason() == "Investigating bad emails"

    switch.release()
    assert not switch.is_engaged()


def test_kill_switch_file_without_reason(tmp_path):
    (tmp_path / "KILL_SWITCH").touch()
    assert KillSwitch(tmp_path / "KILL_SWITCH").reason() == "no reason given"
