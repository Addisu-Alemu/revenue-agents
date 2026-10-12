"""The call brief: prepared when a deal reaches Meeting, with lessons, and questions for what's still missing."""

import pytest

from revenue_agents import story
from revenue_agents.agents.call_brief import CallBriefAgent
from revenue_agents.demo import run
from revenue_agents.demo_llm import demo_llm
from revenue_agents.events import Event, EventType
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import ActionType, AutonomyPolicy, Decision
from revenue_agents.models import DealFields, Stage
from revenue_agents.system import build_system

REVIEWER = "you@cool.example"


@pytest.fixture
def system(tmp_path):
    return build_system(demo_llm(), state_dir=tmp_path)


def to_meeting(system):
    steps = story.plan(system.crm.leads(), "brand")
    story.run(system, steps[0])
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", True, REVIEWER))
    story.run(system, steps[1])  # Maya's question
    system.orchestrator.handle(story.approval_event("send_email:D-001:reply:R-000", True, REVIEWER))
    return story.run(system, steps[2]), steps  # her meeting request


def briefs(system, deal_id="D-001"):
    return [a for a in system.crm.timeline(deal_id) if a.kind == "call_brief"]


def test_brief_is_prepared_when_the_deal_reaches_meeting(system):
    lines, _ = to_meeting(system)

    assert system.crm.get_deal("D-001").stage is Stage.MEETING
    assert "Call brief ready for the rep: BrightBite Burgers, meeting with Maya Thompson -> executed" in lines
    assert lines.index("Stage replied -> meeting") < lines.index(
        "Call brief ready for the rep: BrightBite Burgers, meeting with Maya Thompson -> executed")
    assert len(briefs(system)) == 1


def test_brief_contains_what_we_know_and_what_to_ask(system):
    to_meeting(system)
    brief = briefs(system)[0].data["brief"]

    assert brief["headline"].startswith("Tier A · 97")
    assert "Maya Thompson, VP of Marketing" in brief["attendees"][0]
    assert any("420 locations" in line for line in brief["why_fit"])
    assert any("60 new franchise locations" in line for line in brief["why_fit"])
    assert any("CFO to join" in line for line in brief["what_they_said"])
    assert "Activate: Franchise & Location Targeting" in brief["products"]  # from brand.yaml
    assert brief["questions"] == [
        "What budget is set aside for this?", "Which products, and how many locations, for a first phase?",
        "When should the campaign launch?", "How long should the first phase run?",
        "Who signs the contract, and who else needs to approve it?"]


def test_lessons_feed_the_brief_and_their_use_is_recorded(system):
    to_meeting(system)
    brief = briefs(system)[0].data["brief"]

    assert brief["lessons"] == ["Franchise marketers respond to the pain of slow local versions of national ads. "
                                "(from reply R-001)"]
    [use] = system.lessons.uses("lesson:reply:R-001")
    assert use.used_in == "prepare_call_brief:D-001:meeting"


def test_no_brief_without_a_meeting_request(system, replies):
    system.orchestrator.handle(Event(id="lead:L-002", type=EventType.LEAD_CREATED, payload={"lead_id": "L-002"}))
    system.orchestrator.handle(story.reply_event(replies["R-003"]))  # Owen: not now
    assert briefs(system, "D-002") == []


def test_duplicate_reply_does_not_make_a_second_brief(system):
    to_meeting(system)  # the story delivers R-001 twice
    assert len(briefs(system)) == 1


def test_questions_only_for_what_is_still_missing(leads, segments, make_deal):
    deal = make_deal(stage=Stage.MEETING).model_copy(update={"fields": DealFields(budget_usd=100000)})
    brief = CallBriefAgent(segments).run(leads["L-001"], deal, [], []).output
    assert "What budget is set aside for this?" not in brief.questions
    assert len(brief.questions) == 4


def test_watch_outs_for_flags_that_matter_on_a_call(leads, segments, make_deal):
    deal = make_deal(stage=Stage.MEETING).model_copy(update={"flags": ["pricing_requested", "possible_prompt_injection",
                                                                        "reply_without_outreach"]})
    brief = CallBriefAgent(segments).run(leads["L-001"], deal, [], []).output
    assert len(brief.watch_outs) == 3
    assert "Watch out" in brief.render()


def test_brief_with_no_data_yet_still_renders(leads, segments, make_deal):
    deal = make_deal(stage=Stage.MEETING, tier=None)
    page = CallBriefAgent(segments).run(leads["L-001"], deal, [], []).output.render()
    assert "Not scored yet." in page
    assert "No company facts yet." in page
    assert "None yet for this segment." in page


def test_brief_is_an_internal_low_risk_action(tmp_path, leads, segments, make_deal):
    action = CallBriefAgent(segments).run(leads["L-001"], make_deal(stage=Stage.MEETING), [], []).actions[0]
    policy = AutonomyPolicy(segments, KillSwitch(tmp_path / "KILL_SWITCH"))
    assert action.type is ActionType.PREPARE_CALL_BRIEF
    assert policy.decide(action, make_deal(stage=Stage.MEETING)).decision is Decision.AUTO
    assert policy.decide(action.model_copy(update={"agent": "outreach"}), make_deal()).decision is Decision.BLOCKED


def test_terminal_demo_shows_the_brief_and_labels_lesson_use(tmp_path):
    lines = []
    run(state_dir=tmp_path / "demo", auto_approve=True, out=lines.append)
    output = "\n".join(lines)
    assert "Call brief for the rep (before the meeting):" in output
    assert "Questions to ask (needed before we can send a proposal)" in output
    assert "used in: D-001 (call brief)" in output
    assert "used in: D-002 (email)" in output
