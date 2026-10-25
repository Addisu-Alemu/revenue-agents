"""The kill switch pauses: nothing runs while it is on, nothing is lost, and approved work resumes on release."""

import pytest

from revenue_agents.demo_llm import demo_llm
from revenue_agents.events import Event, EventType
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.llm import LLMError
from revenue_agents.models import Stage
from revenue_agents.runtime import ScriptedRuntime
from revenue_agents.storage import utc_now
from revenue_agents.system import build_system, reset_state
from scripts import kill_switch as kill_switch_cli
from tests import story

OPS = "ops@cool.example"


@pytest.fixture
def system(tmp_path):
    return build_system(ScriptedRuntime(demo_llm()), state_dir=tmp_path)


def lead(lead_id):
    return Event(id=f"lead:{lead_id}", type=EventType.LEAD_CREATED, payload={"lead_id": lead_id})


def io_waiting_for_approval(system, transcript):
    """BrightBite up to the point where sending the IO needs a human."""
    system.orchestrator.handle(lead("L-001"))
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", True, OPS))
    system.orchestrator.handle(Event(id="call:1", type=EventType.CALL_COMPLETED, payload={
        "lead_id": "L-001", "transcript_id": "L-001_discovery_call", "transcript": transcript}))
    assert [r.id for r in system.approvals.pending()] == ["send_io:D-001:IO-D-001"]


def signature_step(system):
    return next(step for step in story.plan(system.crm.leads(), "brand") if step.kind == "signature")


# --- Pause and resume ------------------------------------------------------------------


def test_held_events_replay_in_arrival_order(system):
    system.orchestrator.pause("Investigating", by=OPS)
    for lead_id in ("L-003", "L-002"):
        assert system.orchestrator.handle(lead(lead_id)).status == "held"

    steps = system.orchestrator.resume(by=OPS)

    assert steps[0] == "Kill switch OFF (by ops@cool.example). Replaying 2 held event(s)."
    received = [e.action_id for e in system.audit.events() if e.event == "event_received"]
    assert received == ["lead:L-003", "lead:L-002"]
    assert {d.id: d.stage for d in system.crm.deals()} == {"D-003": Stage.SCORED, "D-002": Stage.OUTREACH}


def test_same_event_delivered_twice_while_paused_is_held_once(system):
    system.orchestrator.pause("Investigating", by=OPS)
    system.orchestrator.handle(lead("L-002"))
    system.orchestrator.handle(lead("L-002"))
    assert len(system.orchestrator.held_events()) == 1


def test_pause_and_resume_are_audited(system):
    system.orchestrator.pause("Bad emails", by=OPS)
    system.orchestrator.resume(by=OPS)
    events = [(e.event, e.detail) for e in system.audit.events()]
    assert ("kill_switch_engaged", "Bad emails (by ops@cool.example)") in events
    assert ("kill_switch_released", "by ops@cool.example") in events


def test_digest_still_works_while_paused_and_shows_what_waits(system, transcript):
    io_waiting_for_approval(system, transcript)
    system.orchestrator.pause("Investigating", by=OPS)
    system.orchestrator.handle(story.approval_event("send_io:D-001:IO-D-001", True, OPS))
    system.orchestrator.handle(lead("L-002"))

    _, digest = story.scheduled_events(0)
    report = system.orchestrator.handle(digest)

    assert report.status == "processed"
    assert "  - Send IO-D-001 ($250,000) to BrightBite Burgers (use Run now)" in report.steps
    assert "  - lead.created (lead:L-002)" in report.steps


def test_one_bad_held_event_does_not_block_the_rest(system):
    """Found in review: an event for an unknown lead raised on replay, and every later resume stuck at it."""
    system.orchestrator.pause("Investigating", by=OPS)
    for lead_id in ("L-999", "L-002"):
        system.orchestrator.handle(lead(lead_id))

    steps = system.orchestrator.resume(by=OPS)

    assert any(step.startswith("lead.created lead:L-999: failed (KeyError") for step in steps)
    assert system.crm.get_deal("D-002").stage is Stage.OUTREACH  # the good event behind it still ran
    assert system.orchestrator.held_events() == []
    assert not system.kill_switch.is_engaged()
    digest = system.orchestrator._tracker.digest(utc_now())
    assert digest.failed_events == ["lead.created (lead:L-999)"]
    assert "Failed on replay (1):" in digest.render()


def test_a_model_failure_on_replay_keeps_the_order_and_pauses_again(system, monkeypatch):
    system.orchestrator.pause("Investigating", by=OPS)
    for lead_id in ("L-002", "L-003"):
        system.orchestrator.handle(lead(lead_id))

    def model_down(task, on_result=None):
        raise LLMError("The model call failed: overloaded")

    monkeypatch.setattr(system.orchestrator._agents.host, "run", model_down)
    steps = system.orchestrator.resume(by=OPS)

    assert system.kill_switch.is_engaged()
    assert "Replay stopped at lead:L-002" in system.kill_switch.reason()
    assert [event.id for event in system.orchestrator.held_events()] == ["lead:L-002", "lead:L-003"]
    assert steps[-1].startswith("Kill switch ON: Replay stopped at lead:L-002")


def test_a_rejection_with_a_note_while_paused_redrafts_on_release(system):
    """Found in review: the redraft and the lesson were blocked by the gateway and lost."""
    system.orchestrator.handle(lead("L-001"))
    asked = len(system.runtime.answers.calls)
    system.orchestrator.pause("Investigating", by=OPS)

    report = system.orchestrator.handle(story.approval_event(
        "send_email:D-001:first_touch", False, OPS, "Mention their Q1 menu launch", save_lesson=True))

    assert system.approvals.get("send_email:D-001:first_touch").status.value == "rejected"  # the decision stands
    assert "Agents paused: approval.decided held until release, nothing lost" in report.steps
    assert system.approvals.pending() == [] and system.lessons.all() == []
    assert len(system.runtime.answers.calls) == asked  # no model call while paused

    system.orchestrator.resume(by=OPS)

    assert [r.id for r in system.approvals.pending()] == ["send_email:D-001:first_touch:v2"]
    assert [lesson.source for lesson in system.lessons.all()] == ["review"]


# --- Approvals that couldn't run (the "orphan" bug) --------------------------------------


def test_approval_while_paused_runs_on_release_and_signature_works(system, transcript):
    io_waiting_for_approval(system, transcript)
    system.orchestrator.pause("Investigating", by=OPS)

    steps = system.orchestrator.handle(story.approval_event("send_io:D-001:IO-D-001", True, OPS)).steps
    assert "-> blocked" in steps[0] and "it will run when the kill switch is released" in steps[0]
    assert [r.id for r in system.orchestrator.approved_waiting()] == ["send_io:D-001:IO-D-001"]
    with pytest.raises(story.NotReadyError, match="approved but wasn't sent"):
        story.run(system, signature_step(system))

    system.orchestrator.resume(by=OPS)

    assert system.crm.get_deal("D-001").stage is Stage.SIGNATURE
    assert system.orchestrator.approved_waiting() == []
    story.run(system, signature_step(system))
    assert system.crm.get_deal("D-001").stage is Stage.HANDOFF


def test_retry_after_a_failure_runs_instead_of_saying_already_approved(system, transcript):
    io_waiting_for_approval(system, transcript)
    handlers = system.gateway._handlers
    original = handlers[ActionType.SEND_IO]

    def timeout(action):
        raise ConnectionError("e-sign service timed out")

    handlers[ActionType.SEND_IO] = timeout
    with pytest.raises(ConnectionError):
        system.orchestrator.handle(story.approval_event("send_io:D-001:IO-D-001", True, OPS))
    handlers[ActionType.SEND_IO] = original

    steps = system.orchestrator.handle(story.approval_event("send_io:D-001:IO-D-001", True, OPS)).steps
    assert "-> executed" in steps[0]
    assert system.crm.get_deal("D-001").stage is Stage.SIGNATURE


def test_run_now_executes_a_waiting_approval_once(system, transcript):
    io_waiting_for_approval(system, transcript)
    system.kill_switch.engage("blocked")  # gateway blocks the run, the decision stays approved
    system.orchestrator.handle(story.approval_event("send_io:D-001:IO-D-001", True, OPS))
    system.kill_switch.release()

    first = system.orchestrator.run_approved("send_io:D-001:IO-D-001")
    second = system.orchestrator.run_approved("send_io:D-001:IO-D-001")

    assert "-> executed" in first[0]
    assert "-> duplicate" in second[0]
    assert [a.kind for a in system.crm.timeline("D-001")].count("io_sent") == 1


def test_a_decision_cannot_be_flipped(system):
    system.orchestrator.handle(lead("L-001"))
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", True, OPS))
    flipped = Event(id="approval:flip", type=EventType.APPROVAL_DECIDED,
                    payload={"request_id": "send_email:D-001:first_touch", "approved": False, "reviewer": "x"})

    assert system.orchestrator.handle(flipped).steps == [
        "Already approved by ops@cool.example: decision not changed"]


def test_run_now_on_a_rejected_request_does_nothing(system):
    system.orchestrator.handle(lead("L-001"))
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", False, OPS))
    assert system.orchestrator.run_approved("send_email:D-001:first_touch") == [
        "send_email:D-001:first_touch is rejected: nothing to run"]


# --- Reset and the command-line switch ----------------------------------------------------


def test_reset_keeps_the_kill_switch_on(tmp_path, system):
    system.orchestrator.pause("Investigating", by=OPS)
    system.orchestrator.handle(lead("L-002"))

    reset_state(tmp_path)

    assert system.kill_switch.is_engaged()
    assert system.orchestrator.held_events() == []  # everything else is gone


def test_reset_of_a_missing_folder_is_harmless(tmp_path):
    reset_state(tmp_path / "nothing-here")


def test_command_line_switch(tmp_path, capsys, system):
    folder = ["--state-dir", str(tmp_path)]
    kill_switch_cli.main(["on", "--reason", "Bad emails", "--by", "maria", *folder])
    assert "Kill switch ON: Bad emails" in capsys.readouterr().out

    system.orchestrator.handle(lead("L-002"))
    kill_switch_cli.main(["status", *folder])
    assert "Held events: 1" in capsys.readouterr().out

    kill_switch_cli.main(["off", "--by", "maria", *folder])
    assert "Replaying 1 held event(s)" in capsys.readouterr().out
    assert system.crm.get_deal("D-002").stage is Stage.OUTREACH
