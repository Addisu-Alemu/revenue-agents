"""Emails are stored as a conversation: our email, the customer's full reply, linked by thread."""

import pytest

from revenue_agents import story
from revenue_agents.demo_llm import demo_llm
from revenue_agents.models import Stage
from revenue_agents.system import build_system

YOU = "you@cool.example"


@pytest.fixture
def system(tmp_path):
    return build_system(demo_llm(), state_dir=tmp_path)


def maya_emailed(system):
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-001")))
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", True, YOU))


def test_sent_email_is_stored_as_an_outbound_message(system):
    maya_emailed(system)
    [message] = system.crm.messages("D-001")

    assert (message.id, message.thread_id, message.direction) == ("msg:send_email:D-001:first_touch", "T-D-001",
                                                                  "outbound")
    assert message.to == "maya.thompson@brightbite.example"
    assert message.body.startswith("Hi Maya,")
    sent = [a for a in system.crm.timeline("D-001") if a.kind == "email_sent"][0]
    assert sent.data["message_id"] == message.id


def test_reply_is_stored_with_its_full_text_and_linked_to_our_email(system, replies):
    maya_emailed(system)
    lines = system.orchestrator.handle(story.reply_event(replies["R-001"])).steps
    ours, theirs = system.crm.messages("D-001")

    assert theirs.direction == "inbound"
    assert theirs.thread_id == ours.thread_id == "T-D-001"
    assert theirs.in_reply_to == ours.id
    assert "I'd like our CFO, Ken, to join" in theirs.body  # her actual words, not just a label
    assert theirs.meta["intent"] == "meeting_request"
    assert "Linked to our email msg:send_email:D-001:first_touch in thread T-D-001" in lines


def test_reply_naming_a_specific_email_links_to_exactly_that_one(system, replies):
    maya_emailed(system)
    reply = replies["R-001"].model_copy(update={"in_reply_to": "msg:send_email:D-001:first_touch"})
    system.orchestrator.handle(story.reply_event(reply))
    assert system.crm.messages("D-001")[1].in_reply_to == "msg:send_email:D-001:first_touch"


def test_reply_after_a_redraft_links_to_the_email_actually_sent(system, replies):
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-001")))
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", False, YOU,
                                                    "Mention their Q1 menu launch"))
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch:v2", True, YOU))
    system.orchestrator.handle(story.reply_event(replies["R-001"]))

    ours, theirs = system.crm.messages("D-001")
    assert ours.id == "msg:send_email:D-001:first_touch:v2"
    assert theirs.in_reply_to == ours.id


def test_reply_we_cannot_link_is_stored_but_flagged(system, replies):
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-001")))  # email still waiting for approval
    system.orchestrator.handle(story.reply_event(replies["R-001"]))

    [message] = system.crm.messages("D-001")
    assert message.direction == "inbound" and message.in_reply_to is None
    assert "reply_without_outreach" in system.crm.get_deal("D-001").flags
    assert system.crm.get_deal("D-001").stage is Stage.SCORED


def test_a_redelivered_reply_is_stored_once(system, replies):
    maya_emailed(system)
    system.orchestrator.handle(story.reply_event(replies["R-001"]))
    system.orchestrator.handle(story.reply_event(replies["R-001"]))
    assert len(system.crm.messages("D-001")) == 2


def test_conversations_are_kept_per_deal(system):
    maya_emailed(system)
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-002")))  # Tier B: sent automatically
    assert [m.deal_id for m in system.crm.messages("D-002")] == ["D-002"]
    assert len(system.crm.messages("D-001")) == 1
