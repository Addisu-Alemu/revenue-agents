"""Emails are stored as a conversation: our email, the customer's full reply, linked by thread."""

import pytest

from revenue_agents import story
from revenue_agents.demo_llm import REPLY_ANSWERS, demo_llm
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


# --- A question before the meeting: the agent drafts the answer, a human approves it ---------


def maya_asked(system, replies):
    maya_emailed(system)
    return system.orchestrator.handle(story.reply_event(replies["R-000"])).steps


def test_a_product_question_gets_a_drafted_answer_that_waits_for_a_human(system, replies):
    lines = maya_asked(system, replies)
    [request] = system.approvals.pending()

    assert "Question to answer: Can corporate control what franchisees are allowed to change in their local ads?" in lines
    assert request.id == "send_email:D-001:reply:R-000"
    assert request.reason == "Answer to a Tier A lead's reply"
    email = request.action.payload
    assert email["subject"] == "Re: Local ads for every BrightBite Burgers location"
    assert email["body"].startswith("Hi Maya,\n\nThat's the problem we built this for.")
    assert "Would you be open to a 30-minute call next week?" in email["body"]
    assert system.crm.get_deal("D-001").stage is Stage.REPLIED


def test_the_full_conversation_is_one_linked_thread(system, replies):
    maya_asked(system, replies)
    system.orchestrator.handle(story.approval_event("send_email:D-001:reply:R-000", True, YOU))
    system.orchestrator.handle(story.reply_event(replies["R-001"]))
    messages = system.crm.messages("D-001")

    assert [m.direction for m in messages] == ["outbound", "inbound", "outbound", "inbound"]
    assert {m.thread_id for m in messages} == {"T-D-001"}
    assert messages[1].in_reply_to == "msg:send_email:D-001:first_touch"  # her question answers our first email
    assert messages[3].in_reply_to == "msg:send_email:D-001:reply:R-000"  # her meeting request answers our answer
    assert system.crm.get_deal("D-001").stage is Stage.MEETING


def test_a_pricing_question_is_never_answered_by_the_agent(system, replies):
    maya_emailed(system)
    asks_price = replies["R-000"].model_copy(update={"id": "R-090", "body": "Looks good. What's your rate card?"})
    system.orchestrator.handle(story.reply_event(asks_price))

    assert not [r for r in system.approvals.pending() if "reply:" in r.id]
    assert "pricing_requested" in system.crm.get_deal("D-001").flags


def test_the_answer_prompt_limits_claims_and_treats_the_email_as_untrusted(tmp_path, replies):
    llm = demo_llm()
    maya_asked(build_system(llm, state_dir=tmp_path), replies)
    [call] = [c for c in llm.calls if c.schema == "AnswerDraft"]

    assert "Value propositions (the only claims you may make):" in call.prompt
    assert '<untrusted source="reply:R-000">' in call.prompt
    assert "Don't invent features" in call.system


def test_a_concern_phrased_as_a_question_is_answered_too(tmp_path, replies):
    """Found on a real Claude run: it read Maya's question as an objection, so no answer was drafted."""
    llm = demo_llm()
    llm.responses["ReplyClassification"] = {**REPLY_ANSWERS["R-000"], "intent": "objection"}
    system = build_system(llm, state_dir=tmp_path)
    maya_asked(system, replies)

    assert [r.id for r in system.approvals.pending()] == ["send_email:D-001:reply:R-000"]
