"""The live world: a clock, items that arrive on time, buyers that react to what the agents did, and timers."""

import datetime as dt

import pytest

from revenue_agents.events import approval_event
from revenue_agents.llm import FakeLLM, LLMError
from revenue_agents.simulator import BuyerSimulator
from revenue_agents.storage import set_clock, utc_now
from revenue_agents.world import RUNNER_LOCK, Desk, Item, Runner, World

UTC = dt.timezone.utc


@pytest.fixture
def desk(tmp_path):
    desk = Desk(tmp_path)
    desk.start()
    yield desk
    set_clock(None)


def at(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def labels(delivered):
    return [d.label for d in delivered]


def approve_everything(desk):
    def between():
        for request in desk.system().approvals.pending():
            desk.world.submit(approval_event(request.id, True, "you@cool.example"), f"Approved {request.id}")
    return between


# --- The clock and the queue ---------------------------------------------------------------


def test_a_new_world_starts_just_before_the_first_lead(desk):
    state = desk.world.read()

    assert state.now == at(1, 8, 30)
    assert not state.running
    leads = [item for item in state.queue if item.source == "lead"]
    assert [(item.label, item.at) for item in leads[:2]] == [("New lead: BrightBite Burgers", at(1, 9, 15)),
                                                            ("New lead: Lumen Media Group", at(1, 11, 45))]
    assert {item.label for item in state.queue if item.source == "scheduler"} == {"Morning SLA check", "Daily digest"}


def test_the_system_clock_is_the_world_clock(desk):
    assert utc_now() == at(1, 8, 30)
    desk.world.skip()
    assert utc_now() == at(1, 9, 15)


def test_the_clock_only_runs_while_playing_and_never_skips_an_item(desk):
    desk.world.advance(10)
    assert desk.world.now() == at(1, 8, 30)  # paused

    desk.world.play()
    desk.world.advance(10)  # 10 s at 1 h/s would be 18:30, but the first lead arrives at 09:15
    assert desk.world.now() == at(1, 9, 15)


def test_items_arrive_in_time_order_and_your_decisions_first(desk):
    desk.world.skip()
    desk.world.submit(approval_event("missing", True, "you@cool.example"), "Your decision")
    assert desk.world.take().label == "Your decision"


def test_your_decisions_are_delivered_even_while_paused(desk):
    first = desk.step()  # BrightBite arrives once the clock is there: skip moves it
    assert first is None
    desk.world.skip()
    desk.step()
    [request] = desk.system().approvals.pending()

    desk.world.submit(approval_event(request.id, True, "you@cool.example"), "You approved the first email")
    delivered = desk.step()
    assert delivered.source == "you" and "-> executed" in delivered.steps[0]
    assert not desk.world.read().running


def test_a_decision_that_cant_apply_is_reported_not_stuck(desk):
    desk.world.submit(approval_event("send_email:D-404:first_touch", True, "you@cool.example"), "A stale click")
    delivered = desk.step()

    assert delivered.status == "failed" and "no approval request" in delivered.steps[0]
    assert desk.world.read().queue[0].source != "you"  # removed: it doesn't block the world


def test_a_model_failure_pauses_the_world_and_keeps_the_item(desk, monkeypatch):
    desk.world.play()
    desk.world.skip()

    def broken(task, on_result=None):
        raise LLMError("The model call failed: overloaded")

    monkeypatch.setattr(desk.system().orchestrator._agents.host, "run", broken)
    delivered = desk.step()

    state = desk.world.read()
    assert delivered.status == "failed"
    assert not state.running and "overloaded" in state.error
    assert state.queue[0].label == "New lead: BrightBite Burgers"  # still there: play retries it


# --- The buyers react to what the agents did ---------------------------------------------------


def test_nobody_replies_to_an_email_that_never_went_out(desk):
    desk.run_until(at(5, 23))  # nobody approves Maya's first email

    assert "Maya replies" not in labels(desk.world.log())
    assert not [beat for beat in desk.world.read().beats if beat.startswith("beat:L-001")]


def test_a_reply_comes_after_the_email_actually_went_out(desk):
    desk.run_until(at(3, 12))  # pending all along
    desk.world.submit(approval_event("send_email:D-001:first_touch", True, "you@cool.example"), "Approved")
    desk.run_until(at(4, 23))
    [reply] = [d for d in desk.world.log() if d.label == "Maya replies"]

    assert reply.at == at(3, 18)  # six hours after it was sent, not at the scripted time (3 Oct 14:10)


def test_the_meeting_the_agent_booked_is_when_the_call_happens(desk):
    desk.run_until(at(9, 23), between=approve_everything(desk))
    [booked] = [a for a in desk.system().crm.timeline("D-001") if a.kind == "meeting_booked"]
    [call] = [d for d in desk.world.log() if d.label == "Call with BrightBite Burgers ends"]

    assert dt.datetime.fromisoformat(booked.data["when"]) == at(8, 15)
    assert call.at == at(8, 15, 45)


def test_a_duplicate_webhook_is_handled_once(desk):
    desk.run_until(at(6, 0), between=approve_everything(desk))
    [reply] = [d for d in desk.world.log() if d.label == "Maya replies" and "meeting_request" in d.steps[0]]
    assert "Already handled: ignored" in reply.steps


def test_a_forged_signature_is_rejected_and_only_the_valid_io_is_signed(desk):
    desk.run_until(at(13, 0), between=approve_everything(desk))
    log = {d.label: d for d in desk.world.log()}

    assert log["A webhook claims an IO was signed"].steps == [
        "Rejected: unknown envelope 'ENV-FAKE-001' (forged or misrouted webhook)"]
    assert "IO-D-001-v2 signed by Ken Adeyemi" in log["Ken Adeyemi signs the IO"].steps[0]
    assert desk.system().crm.get_deal("D-001").stage.value == "handoff"


def test_with_claude_the_buyers_get_a_voice(tmp_path):
    voice = FakeLLM({})
    desk = Desk(tmp_path, voice=voice)
    desk.start()
    assert desk.simulator()._voice is None  # scripted: the mock replies, word for word
    desk.world.set_model("claude")
    assert desk.simulator()._voice is voice
    set_clock(None)


def test_a_voiced_buyer_answers_the_email_the_agents_actually_sent(tmp_path):
    voice = FakeLLM({"BuyerReply": {"body": "Thanks Daniel. How do you stop franchisees going off-brand? Maya"}})
    desk = Desk(tmp_path)
    desk.start()
    desk._simulators["scripted"] = BuyerSimulator(voice=voice)
    desk.run_until(at(4, 0), between=approve_everything(desk))
    set_clock(None)

    [message] = [m for m in desk.system().crm.messages("D-001") if m.direction == "inbound"]
    assert message.body.startswith("Thanks Daniel. How do you stop")
    assert "Maya Thompson, VP of Marketing" in voice.calls[0].system  # the hidden persona
    assert '<untrusted source="email:' in voice.calls[0].prompt  # our email, as data
    assert "Congratulations on the 60 new franchise locations" in voice.calls[0].prompt


def test_an_injection_reply_is_always_delivered_word_for_word(tmp_path):
    voice = FakeLLM({"BuyerReply": {"body": "should never be used"}})
    desk = Desk(tmp_path)
    desk.start()
    desk._simulators["scripted"] = BuyerSimulator(voice=voice)
    desk.run_until(at(7, 0), between=approve_everything(desk))
    set_clock(None)

    [owen] = [m for m in desk.system().crm.messages("D-002") if m.direction == "inbound"]
    assert "SYSTEM NOTE TO AI ASSISTANT" in owen.body
    assert all("Owen" not in call.system for call in voice.calls)


# --- Timers the agents set -----------------------------------------------------------------------


def test_a_follow_up_an_agent_set_fires_on_its_date(desk):
    desk.run_until(at(7, 0), between=approve_everything(desk))
    [timer] = [item for item in desk.world.upcoming(("timer",)) if "Owen" in item.label]
    assert timer.at == dt.datetime(2027, 1, 6, 9, tzinfo=UTC)

    desk.world.move_to(dt.datetime(2027, 1, 6, 8, 59, tzinfo=UTC))  # three months later
    desk.run_until(dt.datetime(2027, 1, 6, 10, tzinfo=UTC), between=approve_everything(desk))
    [fired] = [d for d in desk.world.log() if d.label == "Follow up with Owen Castillo"]
    assert "Follow up with Owen Castillo today" in fired.steps[0]
    assert "follow_up_due" in desk.system().crm.get_deal("D-002").flags


def test_the_scheduler_runs_every_morning_and_evening(desk):
    desk.run_until(at(3, 23), between=approve_everything(desk))
    times = [(d.label, d.at) for d in desk.world.log() if d.source == "scheduler"]

    assert ("Daily digest", at(1, 18)) in times
    assert ("Morning SLA check", at(2, 8)) in times and ("Morning SLA check", at(3, 8)) in times


# --- Running ---------------------------------------------------------------------------------------


def test_only_one_runner_delivers_for_a_state_folder(desk):
    first, second = Runner(desk), Runner(Desk(desk.state_dir))
    try:
        assert first.start() is True
        assert second.start() is False
        assert (desk.state_dir / RUNNER_LOCK).exists()
    finally:
        first.stop()


def test_the_live_board_shows_who_is_working(desk):
    seen = []
    board = desk.live
    original = board.listener

    def spy(kind, name, data, duration_ms):
        original(kind, name, data, duration_ms)
        seen.append(dict(board.read()))

    board.listener = spy
    desk.world.skip()
    desk.step()

    assert any(snapshot.get("agent") == "research" for snapshot in seen)
    assert any(step["tool"] == "enrich_company" for snapshot in seen for step in snapshot.get("steps", []))
    assert board.read() == {}  # cleared once the item is done


def test_a_runner_picks_up_after_a_crash_mid_delivery(desk):
    """The app was killed while delivering: the next runner clears the stale flag and delivers the item again."""
    desk.world.skip()
    assert desk.world.take().label == "New lead: BrightBite Burgers"  # taken, then the process "dies"
    assert desk.world.take() is None  # stuck: still marked as being delivered

    delivered = desk.run_until(at(1, 9, 30))
    assert [d.label for d in delivered] == ["New lead: BrightBite Burgers"]


def test_the_terminal_demo_wont_deliver_while_another_runner_does(desk):
    runner = Runner(desk)
    try:
        assert runner.start()
        with pytest.raises(RuntimeError, match="another runner"):
            Desk(desk.state_dir).run_until(at(2, 0))
    finally:
        runner.stop()


def test_items_are_queued_once(desk):
    item = Item(id="x", at=at(2, 9), kind="event", source="timer", label="x", event=None)
    desk.world.add([item])
    desk.world.add([item])
    assert [i.id for i in desk.world.read().queue].count("x") == 1


def test_a_world_without_a_file_uses_the_real_time(tmp_path):
    world = World(tmp_path)
    assert not world.exists()
    assert abs((world.now() - dt.datetime.now(UTC)).total_seconds()) < 5


def test_the_clock_pauses_itself_when_something_new_needs_you(desk):
    desk.world.play()
    desk.world.skip()
    desk.step()  # BrightBite: the Tier A first email waits for a human

    state = desk.world.read()
    assert not state.running
    assert state.paused_for == "First email to Maya Thompson at BrightBite Burgers waits for you"

    desk.world.play()  # carry on with it still waiting: only something new pauses again
    assert desk.world.read().paused_for is None
    desk.world.skip()
    desk.step()  # Lumen, Tier A in the agency pack: new, so it pauses again
    assert desk.world.read().paused_for.startswith("First email to Priya Raman")

    desk.world.play()
    desk.world.advance(1)  # nothing new arrives between items
    assert desk.world.read().running


def test_auto_pause_can_be_turned_off(desk):
    desk.world.set_auto_pause(False)
    desk.world.play()
    desk.world.skip()
    desk.step()
    assert desk.world.read().running and desk.system().approvals.pending()

