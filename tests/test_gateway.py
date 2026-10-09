"""Every action goes through the gateway: policy, then execute, queue or block. Always audited."""

import pytest

from revenue_agents.guardrails.approvals import ApprovalQueue
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.gateway import ActionGateway, Outcome
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import Action, ActionType, AutonomyPolicy
from revenue_agents.models import Tier
from revenue_agents.segments import SegmentRegistry


class RecordingHandler:
    """Stands in for a real side effect (sending an email) and remembers each call."""

    def __init__(self):
        self.calls = []

    def __call__(self, action):
        self.calls.append(action.id)
        return f"done:{action.id}"


@pytest.fixture
def setup(tmp_path):
    kill_switch = KillSwitch(tmp_path / "KILL_SWITCH")
    approvals = ApprovalQueue(tmp_path / "approvals.json")
    audit = AuditLog(tmp_path / "audit.jsonl")
    send_email = RecordingHandler()
    gateway = ActionGateway(
        AutonomyPolicy(SegmentRegistry(), kill_switch),
        approvals,
        audit,
        handlers={ActionType.SEND_EMAIL: send_email},
        executed_path=tmp_path / "executed.json",
    )
    return gateway, approvals, audit, kill_switch, send_email


def email(action_id="email:D-001:first"):
    return Action(id=action_id, type=ActionType.SEND_EMAIL, agent="outreach", deal_id="D-001",
                  summary="First email", payload={"subject": "Hi", "body": "Open to a call?"})


def test_auto_action_executes_and_is_audited(setup, make_deal):
    gateway, _, audit, _, send_email = setup
    result = gateway.submit(email(), make_deal(tier=Tier.B))

    assert result.outcome is Outcome.EXECUTED
    assert result.result == "done:email:D-001:first"
    assert send_email.calls == ["email:D-001:first"]
    assert [event.event for event in audit.events()] == ["policy_auto", "executed"]


def test_high_risk_action_is_queued_not_executed(setup, make_deal):
    gateway, approvals, _, _, send_email = setup
    result = gateway.submit(email(), make_deal(tier=Tier.A))

    assert result.outcome is Outcome.QUEUED
    assert send_email.calls == []
    assert [request.id for request in approvals.pending()] == ["email:D-001:first"]


def test_approved_action_executes(setup, make_deal):
    gateway, approvals, audit, _, send_email = setup
    deal = make_deal(tier=Tier.A)
    gateway.submit(email(), deal)
    approvals.approve("email:D-001:first", reviewer="maria@cool.ai")

    result = gateway.execute_approved("email:D-001:first", deal)

    assert result.outcome is Outcome.EXECUTED
    assert send_email.calls == ["email:D-001:first"]
    assert audit.events()[-1].detail == "Approved by maria@cool.ai"


@pytest.mark.parametrize("decide", ["none", "reject"])
def test_unapproved_action_cannot_execute(setup, make_deal, decide):
    gateway, approvals, _, _, send_email = setup
    deal = make_deal(tier=Tier.A)
    gateway.submit(email(), deal)
    if decide == "reject":
        approvals.reject("email:D-001:first", reviewer="maria@cool.ai")

    with pytest.raises(PermissionError):
        gateway.execute_approved("email:D-001:first", deal)
    assert send_email.calls == []


def test_kill_switch_stops_even_approved_actions(setup, make_deal):
    gateway, approvals, _, kill_switch, send_email = setup
    deal = make_deal(tier=Tier.A)
    gateway.submit(email(), deal)
    approvals.approve("email:D-001:first", reviewer="maria@cool.ai")
    kill_switch.engage("Pause all sends")

    assert gateway.execute_approved("email:D-001:first", deal).outcome is Outcome.BLOCKED
    assert send_email.calls == []


def test_blocked_action_never_reaches_handler(setup, make_deal):
    gateway, _, audit, kill_switch, send_email = setup
    kill_switch.engage("stop")

    assert gateway.submit(email(), make_deal(tier=Tier.B)).outcome is Outcome.BLOCKED
    assert send_email.calls == []
    assert audit.events()[-1].event == "policy_blocked"


def test_retried_action_runs_only_once(setup, make_deal):
    """A webhook delivered twice must not send the email twice."""
    gateway, _, _, _, send_email = setup
    deal = make_deal(tier=Tier.B)

    gateway.submit(email(), deal)
    second = gateway.submit(email(), deal)

    assert second.outcome is Outcome.DUPLICATE
    assert send_email.calls == ["email:D-001:first"]


def test_approved_action_runs_only_once(setup, make_deal):
    gateway, approvals, _, _, send_email = setup
    deal = make_deal(tier=Tier.A)
    gateway.submit(email(), deal)
    approvals.approve("email:D-001:first", reviewer="maria@cool.ai")

    gateway.execute_approved("email:D-001:first", deal)
    assert gateway.execute_approved("email:D-001:first", deal).outcome is Outcome.DUPLICATE
    assert send_email.calls == ["email:D-001:first"]


def test_action_without_handler_is_blocked(setup, make_deal):
    gateway, _, _, _, _ = setup
    enrich = Action(id="enrich:D-001", type=ActionType.ENRICH_LEAD, agent="research", deal_id="D-001", summary="Enrich")
    assert gateway.submit(enrich, make_deal()).outcome is Outcome.BLOCKED
