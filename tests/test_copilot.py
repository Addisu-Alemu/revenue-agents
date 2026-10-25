"""The copilot works across deals with the same tools, hook and policy, and can change only two low-risk things."""

import datetime as dt

import pytest

from revenue_agents.events import approval_event
from revenue_agents.guardrails.policy import Action, ActionType, Decision
from revenue_agents.runtime import Verdict
from revenue_agents.storage import set_clock
from revenue_agents.world import Desk
from tests.desk import run


def at(day, hour=0):
    return dt.datetime(2026, 10, day, hour, tzinfo=dt.timezone.utc)


@pytest.fixture
def desk(tmp_path):
    """The world on 9 October, everything approved: Lumen was emailed on 1 October and never replied."""
    desk = Desk(tmp_path)
    desk.start()

    def approve_all():
        for request in desk.system().approvals.pending():
            desk.world.submit(approval_event(request.id, True, "you@cool.example"), f"Approved {request.id}")

    desk.run_until(at(9), between=approve_all)
    yield desk
    set_clock(None)


def ask(desk, goal):
    desk.world.command("ask", f"You asked: {goal}", goal=goal, by="you@cool.example")
    return desk.step()


def test_a_quiet_deal_gets_a_follow_up_timer_and_a_note(desk):
    delivered = ask(desk, "Which deals went quiet after our first email? Follow up")
    system = desk.system()

    assert "The copilot: Lumen Media Group: follow-up set for Sun 11 Oct and flagged for the rep." in delivered.steps
    assert "copilot_note" in system.crm.get_deal("D-004").flags
    [timer] = [item for item in desk.world.upcoming(("timer",)) if "Priya" in item.label]
    assert timer.at == dt.datetime(2026, 10, 11, 9, tzinfo=dt.timezone.utc)  # the world will fire it


def test_it_leaves_alone_what_the_buyer_already_set_a_date_for(desk):
    delivered = ask(desk, "Follow up with every deal that went quiet after a pricing question")
    assert delivered.steps[-1] == ("The copilot: Copperline Creative: skipped, a follow-up is already set for "
                                   "2027-01-05. Harbor Lane Home: skipped, a follow-up is already set for 2027-01-06.")


def test_rules_in_code_refuse_what_it_shouldn_t_do(desk):
    system = desk.system()
    task = system.orchestrator._agents.copilot.task("test", "ask:test")
    task.script = lambda s: [s.call("set_follow_up", deal_id=deal, on="2026-10-12", reason="test")
                             for deal in ("D-003", "D-005", "D-001")]
    calls = run(system, task).calls

    assert [(call.verdict, call.reason) for call in calls[:2]] == [
        (Verdict.DENY, "D-003 was never contacted: nothing to follow up"),  # Tier C, left for nurture
        (Verdict.DENY, "D-005 already has a follow-up set for 2027-01-05")]  # the buyer set the date
    assert (calls[2].verdict, calls[2].outcome.value) == (Verdict.ALLOW, "executed")  # BrightBite: allowed


def test_it_may_not_send_email_or_touch_contracts(desk):
    system = desk.system()
    deal = system.crm.get_deal("D-001")
    for kind in (ActionType.SEND_EMAIL, ActionType.SEND_IO, ActionType.UPDATE_DEAL):
        action = Action(id=f"{kind.value}:D-001:copilot", type=kind, agent="copilot", deal_id="D-001", summary="x",
                        payload={})
        decision = system.gateway.check(action, deal)
        assert (decision.decision, decision.rule) == (Decision.BLOCKED, "agent_scope")


def test_a_tool_outside_its_list_is_refused(desk):
    system = desk.system()
    task = system.orchestrator._agents.copilot.task("test", "ask:test")
    task.script = lambda s: s.call("send_email")
    [call] = run(system, task).calls
    assert (call.verdict, call.rule) == (Verdict.DENY, "scoped_tools")
