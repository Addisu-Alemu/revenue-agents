"""Rejecting an email with feedback produces a redraft; feedback can become a lesson for future emails."""

import pytest

from revenue_agents import story
from revenue_agents.demo import run
from revenue_agents.demo_llm import demo_llm
from revenue_agents.events import Event, EventType
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Stage
from revenue_agents.system import build_system

REVIEWER = "maria@cool.example"
FEEDBACK = "Mention their Q1 menu launch"


@pytest.fixture
def llm():
    return demo_llm()


@pytest.fixture
def system(tmp_path, llm):
    system = build_system(llm, state_dir=tmp_path)
    system.orchestrator.handle(Event(id="lead:L-001", type=EventType.LEAD_CREATED, payload={"lead_id": "L-001"}))
    return system


def reject(system, request_id, note=FEEDBACK, save_lesson=False):
    return system.orchestrator.handle(story.approval_event(request_id, False, REVIEWER, note, save_lesson)).steps


def pending_ids(system):
    return [r.id for r in system.approvals.pending()]


def test_rejecting_with_feedback_creates_a_second_draft_for_approval(system, llm):
    steps = reject(system, "send_email:D-001:first_touch")

    assert steps[0] == 'Rejected by maria@cool.example: send_email:D-001:first_touch ("Mention their Q1 menu launch")'
    assert "Redrafting with the reviewer's feedback (v2)" in steps
    assert pending_ids(system) == ["send_email:D-001:first_touch:v2"]  # still needs a human

    prompt = llm.calls[-1].prompt
    assert "Their feedback (follow it):\nMention their Q1 menu launch" in prompt
    assert "Congratulations on the 60 new franchise locations" in prompt  # the rejected draft
    new_body = system.approvals.pending()[0].action.payload["body"]
    assert "national menu launch coming in Q1" in new_body


def test_approving_the_redraft_sends_it(system):
    reject(system, "send_email:D-001:first_touch")
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch:v2", True, REVIEWER))

    assert system.crm.get_deal("D-001").stage is Stage.OUTREACH
    [sent] = [a for a in system.crm.timeline("D-001") if a.kind == "email_sent"]
    assert "national menu launch coming in Q1" in sent.data["body"]


def test_rejecting_without_feedback_does_not_redraft(system):
    reject(system, "send_email:D-001:first_touch", note=None)
    assert pending_ids(system) == []
    assert system.crm.get_deal("D-001").stage is Stage.SCORED


def test_feedback_saved_as_a_lesson_feeds_later_emails(system, llm):
    reject(system, "send_email:D-001:first_touch", note="Lead with the operational pain, not congratulations",
           save_lesson=True)

    [lesson] = system.lessons.all()
    assert (lesson.source, lesson.source_id) == ("review", "send_email:D-001:first_touch")
    assert lesson.text == "Lead with the operational pain, not congratulations"

    system.orchestrator.handle(Event(id="lead:L-002", type=EventType.LEAD_CREATED, payload={"lead_id": "L-002"}))
    assert "[lesson:review:send_email:D-001:first_touch] Lead with the operational pain" in llm.calls[-1].prompt


def test_feedback_is_not_a_lesson_unless_asked(system):
    reject(system, "send_email:D-001:first_touch")
    assert system.lessons.all() == []


def test_two_redrafts_then_a_human_writes_it(system):
    reject(system, "send_email:D-001:first_touch")
    reject(system, "send_email:D-001:first_touch:v2")
    steps = reject(system, "send_email:D-001:first_touch:v3")

    assert pending_ids(system) == []
    assert "needs_manual_email" in system.crm.get_deal("D-001").flags
    assert any("3 drafts rejected; a human should write it" in step for step in steps)


def test_rejected_io_feedback_can_be_a_lesson_without_a_redraft(tmp_path, transcript):
    system = build_system(demo_llm(), state_dir=tmp_path)
    handle = system.orchestrator.handle
    handle(Event(id="lead:L-001", type=EventType.LEAD_CREATED, payload={"lead_id": "L-001"}))
    handle(story.approval_event("send_email:D-001:first_touch", True, REVIEWER))
    handle(Event(id="call:1", type=EventType.CALL_COMPLETED, payload={
        "lead_id": "L-001", "transcript_id": "L-001_discovery_call", "transcript": transcript}))

    reject(system, "send_io:D-001:IO-D-001", note="Franchise pilots need a 30-day exit clause", save_lesson=True)

    assert pending_ids(system) == []
    assert system.lessons.for_segment("brand")[0].text == "Franchise pilots need a 30-day exit clause"


def test_a_failed_redraft_can_be_retried_without_duplicates(system):
    outreach = system.orchestrator._agents.outreach
    original = outreach.run
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("model timed out")
        return original(*args, **kwargs)

    outreach.run = flaky
    with pytest.raises(TimeoutError):
        reject(system, "send_email:D-001:first_touch")
    reject(system, "send_email:D-001:first_touch")  # the webhook / click is retried

    assert pending_ids(system) == ["send_email:D-001:first_touch:v2"]


def test_terminal_demo_asks_for_feedback_and_shows_the_redraft(tmp_path):
    answers = iter(["n", FEEDBACK, "y"] + ["y"] * 10)  # reject, give feedback, save as lesson, approve the rest
    lines = []
    assert run(state_dir=tmp_path / "demo", ask=lambda prompt: next(answers), out=lines.append) == 0
    output = "\n".join(lines)

    assert "Redrafting with the reviewer's feedback (v2)" in output
    assert "APPROVAL NEEDED: First email to Maya Thompson at BrightBite Burgers (draft v2, after review)" in output
    assert "national menu launch coming in Q1" in output
    assert "learned from review send_email:D-001:first_touch" in output


def test_redraft_action_ids_are_versioned(system):
    reject(system, "send_email:D-001:first_touch")
    action = system.approvals.pending()[0].action
    assert action.type is ActionType.SEND_EMAIL
    assert action.summary == "First email to Maya Thompson at BrightBite Burgers (draft v2, after review)"


def test_a_redraft_that_breaks_the_rules_flags_the_deal(tmp_path, llm):
    scripted = llm.responses["OutreachDraft"]

    def guarantee_on_redraft(call):
        if "Their feedback (follow it)" in call.prompt:
            return {"opening": "We guarantee a Q1 launch.", "value_prop_index": 0}
        return scripted(call)

    system = build_system(type(llm)({**llm.responses, "OutreachDraft": guarantee_on_redraft}), state_dir=tmp_path)
    system.orchestrator.handle(Event(id="lead:L-001", type=EventType.LEAD_CREATED, payload={"lead_id": "L-001"}))
    reject(system, "send_email:D-001:first_touch")

    assert pending_ids(system) == []
    assert "outreach_draft_rejected" in system.crm.get_deal("D-001").flags
