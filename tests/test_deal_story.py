"""The Deal room reads its story, plan and field sources from what the system recorded."""

import datetime as dt

import pytest

from revenue_agents.events import approval_event
from revenue_agents.storage import set_clock
from revenue_agents.ui import deal_story
from revenue_agents.world import Desk


def at(day, hour=0):
    return dt.datetime(2026, 10, day, hour, tzinfo=dt.timezone.utc)


@pytest.fixture
def brightbite(tmp_path):
    """The world up to BrightBite's IO v2 waiting for approval (everything before it approved)."""
    desk = Desk(tmp_path)
    desk.start()

    def approve_all_but_v2():
        for request in desk.system().approvals.pending():
            if not request.id.endswith("IO-D-001-v2"):
                desk.world.submit(approval_event(request.id, True, "you@cool.example"), f"Approved {request.id}")

    desk.run_until(at(12), between=approve_all_but_v2)
    yield desk, desk.system(), desk.system().crm.get_deal("D-001")
    set_clock(None)


def test_the_story_is_told_in_words_with_the_customer_s_own_quotes(brightbite):
    _, system, deal = brightbite
    beats = deal_story.story(system, deal)
    lines = [f"{beat.actor} {beat.text}" for beat in beats]

    assert lines[0] == "Maya Thompson came in as a new lead (inbound form)."
    assert "Research agent found 420 locations" in lines[1] and "Score 97, Tier A." in lines[1]
    assert any(line.startswith("Outreach agent sent the first email") and "you approved it" in line for line in lines)
    question = next(beat for beat in beats if beat.text == "asked a question first.")
    assert question.quote.startswith("We tried a template tool last year")  # not "Thanks for this"
    assert any("booked the call on its own" in line for line in lines)
    assert "Orchestrator kept the deal at meeting: a proposal still needs budget, flight." in lines
    assert [beat.at for beat in beats] == sorted(beat.at for beat in beats)
    assert not any(beat.text.startswith("Stage ") for beat in beats)  # stage moves are on the stage bar


def test_the_plan_puts_what_waits_for_you_first(brightbite):
    desk, system, deal = brightbite
    steps = deal_story.plan(system, desk.world, deal).steps

    assert steps[0][0] is True and steps[0][1].startswith("You decide: Send IO-D-001-v2")
    assert steps[1] == (False, "Ken Adeyemi (CFO) signs; the signature is checked with the e-sign provider, then "
                               "closed won and handed to delivery. A forged webhook is rejected. The morning SLA check "
                               "flags it after 7 days without progress.")


def test_each_field_shows_who_said_it_and_when(brightbite):
    _, system, deal = brightbite
    rows = {row.label: row for row in deal_story.fields(system, deal)}

    assert (rows["Budget"].value, rows["Budget"].source) == ("$250,000", "Ken, discovery call 00:01:30")
    assert rows["Budget"].quote == "we have about 250 thousand dollars for Q1"
    assert rows["Flight"].value == "15 Jan to 15 Apr 2027"
    assert rows["Signs"].source == "Ken, discovery call 00:09:30"


def test_a_term_changed_later_says_how_it_changed(brightbite):
    desk, system, deal = brightbite
    desk.world.submit(approval_event("send_io:D-001:IO-D-001-v2", True, "you@cool.example"), "Approved v2")
    desk.step()
    payment = next(row for row in deal_story.fields(system, system.crm.get_deal("D-001")) if row.label == "Payment")

    assert payment.value == "Net 45"
    assert (payment.source, payment.quote) == ("Terms agreed in IO-D-001-v2 (asked in R-004)", None)


def test_a_lead_that_waited_for_the_kill_switch_says_so(tmp_path):
    desk = Desk(tmp_path)
    desk.start()
    system = desk.system()
    system.orchestrator.pause("Checking", by="ops@cool.example")
    desk.run_until(at(1, 12))
    desk.world.command("resume", "You released the agents", by="ops@cool.example")
    desk.step()
    beats = [f"{beat.at:%H:%M} {beat.actor} {beat.text}" for beat in deal_story.story(system, system.crm.get_deal("D-001"))]
    set_clock(None)

    assert "09:15 Kill switch agents were paused, so the new lead waited. Nothing was lost." in beats
    assert any(beat.startswith("12:00 You released the agents (by ops@cool.example)") for beat in beats)
    assert beats.index(next(b for b in beats if "Research agent" in b)) > beats.index(
        next(b for b in beats if b.startswith("12:00 You released")))
