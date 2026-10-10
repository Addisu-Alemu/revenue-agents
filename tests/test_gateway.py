"""Every action goes through the gateway: policy, then execute, queue or block. Always audited."""

import threading
import time

import pytest

from revenue_agents.guardrails.approvals import ApprovalQueue
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.gateway import ActionGateway, Outcome
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import Action, ActionType, AutonomyPolicy
from revenue_agents.models import Tier
from revenue_agents.segments import SegmentRegistry


class RecordingHandler:
    """Stands in for a real side effect (sending an email) and remembers each call."""

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    def __call__(self, action):
        time.sleep(self.delay)  # a slow send widens the window for a race
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
        claims=ActionClaims(tmp_path / "claims"),
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


# --- Concurrency and failures ------------------------------------------------


def run_at_once(count, target):
    """Start `count` threads that all call target(i) at the same instant."""
    barrier = threading.Barrier(count)

    def worker(i):
        barrier.wait()
        target(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_same_action_delivered_concurrently_runs_once(setup, make_deal):
    """Two workers receive the same webhook at the same moment: only one email goes out."""
    gateway, _, _, _, send_email = setup
    send_email.delay = 0.05
    deal = make_deal(tier=Tier.B)
    outcomes = []

    run_at_once(10, lambda i: outcomes.append(gateway.submit(email(), deal).outcome))

    assert send_email.calls == ["email:D-001:first"]
    assert outcomes.count(Outcome.EXECUTED) == 1
    assert outcomes.count(Outcome.DUPLICATE) == 9


def test_concurrent_different_actions_are_all_remembered(setup, make_deal):
    """No executed id is lost when many actions finish at the same moment."""
    gateway, _, _, _, send_email = setup
    deal = make_deal(tier=Tier.B)

    run_at_once(20, lambda i: gateway.submit(email(f"email:D-001:{i}"), deal))
    assert len(send_email.calls) == 20

    retries = [gateway.submit(email(f"email:D-001:{i}"), deal).outcome for i in range(20)]
    assert set(retries) == {Outcome.DUPLICATE}
    assert len(send_email.calls) == 20


def test_failed_action_can_be_retried(tmp_path, setup, make_deal):
    gateway, _, audit, _, send_email = setup
    deal = make_deal(tier=Tier.B)
    attempts = []

    def flaky(action):
        attempts.append(action.id)
        if len(attempts) == 1:
            raise ConnectionError("email provider timed out")
        return "sent"

    gateway._handlers[ActionType.SEND_EMAIL] = flaky

    with pytest.raises(ConnectionError):
        gateway.submit(email(), deal)
    assert audit.events()[-1].event == "failed"

    assert gateway.submit(email(), deal).outcome is Outcome.EXECUTED
    assert len(attempts) == 2
