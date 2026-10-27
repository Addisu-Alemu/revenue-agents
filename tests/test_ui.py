"""The UI shows the live world and sends every decision through it. No browser needed (Streamlit AppTest).

The background runner is off in these tests (REVENUE_AGENTS_RUNNER=off): each test moves the world itself, so
what a page shows and what a click does can be checked one step at a time.
"""

import datetime as dt
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from revenue_agents.events import approval_event
from revenue_agents.storage import set_clock
from revenue_agents.ui import live
from revenue_agents.world import Desk, World

ROOT = Path(__file__).resolve().parent.parent
HARNESS = str(ROOT / "tests" / "ui_harness.py")
APP = str(ROOT / "app.py")
MAYA_EMAIL = "send_email:D-001:first_touch"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("REVENUE_AGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REVENUE_AGENTS_RUNNER", "off")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield tmp_path / "state"
    set_clock(None)


def at(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=dt.timezone.utc)


def world_until(state_dir, when, approve=True) -> Desk:
    desk = Desk(state_dir)
    if not desk.world.exists():
        desk.start()

    def approve_all():
        for request in desk.system().approvals.pending():
            desk.world.submit(approval_event(request.id, True, "you@cool.example"), f"Approved {request.id}")

    desk.run_until(when, between=approve_all if approve else None)
    return desk


def page(name):
    test = AppTest.from_file(HARNESS, default_timeout=30)
    test.session_state["page"] = name
    return test.run()


def app():
    return AppTest.from_file(APP, default_timeout=30).run()


def texts(test):
    return "\n".join([m.value for m in test.markdown] + [c.value for c in test.caption] + [t.value for t in test.title]
                     + [h.value for h in test.subheader])


def button(test, label):
    return next(b for b in test.button if b.label.startswith(label))


# --- Today ------------------------------------------------------------------------------------


def test_today_offers_to_start_the_world(isolated_state):
    test = app()
    assert not test.exception
    assert test.title[0].value == "Today"

    button(test, "Start").click().run()
    assert not test.exception
    assert World(isolated_state).read().running


def test_today_shows_what_needs_you_and_what_the_agents_did(isolated_state):
    world_until(isolated_state, at(1, 10), approve=False)
    test = app()
    text = texts(test)

    assert not test.exception
    assert "<h1 class='ra-headline'>1 decision needs you</h1>" in text
    assert "So far your agents took 3 actions on their own." in text
    assert "<div class='ra-need-title'>First email to Maya Thompson at BrightBite Burgers</div>" in text
    assert "Built around: 60 new franchise locations" in text  # why this email, from the research note
    assert "● Rule: first contact with a top-tier lead" in text  # the rule the trace recorded
    assert "<b>Research agent</b> <span class='ra-company'>BrightBite Burgers</span>" in text
    assert "<span class='ra-chip ask'>send_email · asks you</span>" in text
    assert "Thursday 01 October · 10:00 UTC" in text


def test_review_opens_the_inbox_on_that_item(isolated_state):
    world_until(isolated_state, at(1, 10), approve=False)
    test = app()
    button(test, "Review").click().run()

    assert not test.exception
    assert test.session_state["inbox_selected"] == MAYA_EMAIL
    assert test.title[0].value == "Inbox"


def test_pausing_the_agents_holds_what_arrives_and_release_replays_it(isolated_state):
    desk = world_until(isolated_state, at(1, 9))
    test = app()
    button(test, "Pause all agents").click().run()
    assert desk.system().kill_switch.is_engaged()

    desk.world.skip()
    assert desk.step().status == "held"  # BrightBite arrives and waits
    test = app()
    assert "New lead: BrightBite Burgers: waits until you release the agents. Nothing is lost." in texts(test)

    button(test, "Release agents").click().run()
    released = desk.step()
    assert released.label == "You released the agents"
    assert "New lead: BrightBite Burgers (brand) -> deal D-001" in " ".join(released.steps)


# --- Inbox --------------------------------------------------------------------------------------


def test_approving_in_the_inbox_goes_through_the_desk(isolated_state):
    desk = world_until(isolated_state, at(1, 10), approve=False)
    test = page("inbox")
    assert "● Why it stopped: first contact with a top-tier lead" in texts(test)

    button(test, "Approve").click().run()
    assert not test.exception
    [item] = [item for item in desk.world.read().queue if item.source == "you"]
    assert item.label == "Approved by you: First email to Maya Thompson at BrightBite Burgers"

    desk.step()
    system = desk.system()
    assert system.approvals.get(MAYA_EMAIL).status.value == "approved"
    assert [a.kind for a in system.crm.timeline("D-001")][-2:] == ["email_sent", "stage_change"]


def test_after_the_last_decision_you_are_back_on_today(isolated_state, monkeypatch):
    desk = world_until(isolated_state, at(1, 10), approve=False)
    original = live.decide

    def decide_now(world, *args, **kwargs):  # no runner in tests: deliver the decision here
        original(world, *args, **kwargs)
        desk.step()
        return True

    monkeypatch.setattr(live, "decide", decide_now)
    went_to = []
    monkeypatch.setattr(st, "switch_page", went_to.append)
    test = AppTest.from_file(HARNESS, default_timeout=30)
    test.session_state["page"] = "inbox"
    test.session_state["pages"] = {"today": "today"}
    test.run()
    button(test, "Approve").click().run()

    assert not test.exception
    assert went_to == ["today"]
    assert test.session_state["flash"][0] == ("Approved: First email to Maya Thompson at BrightBite Burgers. Nothing "
                                              "else waits for you.")


def test_when_more_waits_the_next_one_opens_in_the_inbox(isolated_state, monkeypatch):
    desk = world_until(isolated_state, at(1, 12), approve=False)  # BrightBite and Lumen wait
    original = live.decide

    def decide_now(world, *args, **kwargs):
        original(world, *args, **kwargs)
        desk.step()
        return True

    monkeypatch.setattr(live, "decide", decide_now)
    went_to = []
    monkeypatch.setattr(st, "switch_page", went_to.append)
    test = page("inbox")
    button(test, "Approve").click().run()

    assert not went_to
    assert test.session_state["flash"][0].endswith("Next: First email to Priya Raman at Lumen Media Group")
    assert "Priya Raman" in texts(test)


def test_the_inbox_says_when_the_segment_filter_hides_what_waits(isolated_state):
    world_until(isolated_state, at(1, 10), approve=False)  # Maya's email waits in the brand pack
    test = page("inbox")
    test.session_state["segment"] = "agency"
    test.run()

    assert "1 waits for you in another segment." in [i.value for i in test.info]
    button(test, "Show all segments").click().run()
    assert test.session_state["segment"] == "all"


def test_coaching_redrafts_and_can_teach_a_lesson(isolated_state):
    desk = world_until(isolated_state, at(1, 10), approve=False)
    test = page("inbox")
    test.text_area(key=f"detail-{MAYA_EMAIL}-note").input("Mention their Q1 menu launch")
    test.checkbox(key=f"detail-{MAYA_EMAIL}-lesson").check()
    test.run()
    button(test, "Redraft").click().run()
    desk.step()

    system = desk.system()
    assert [r.id for r in system.approvals.pending()] == [f"{MAYA_EMAIL}:v2"]
    assert system.lessons.all()[0].source == "review"
    assert any("(draft v2, after review)" in m.value for m in page("inbox").markdown)


def test_the_inbox_shows_what_the_agent_did_and_what_it_used(isolated_state):
    world_until(isolated_state, at(1, 10), approve=False)
    text = texts(page("inbox"))

    assert "<h5>What the agent did</h5><div class='ra-muted'>Outreach agent · scripted answers</div>" in text
    assert [tool for tool in ("read_account", "search_lessons", "draft_email", "send_email")
            if f"<code>{tool}</code>" in text] == ["read_account", "search_lessons", "draft_email", "send_email"]
    assert "<code>policy.first_touch_tier · PreToolUse hook on send_email, then the gateway</code>" in text
    assert "proposed by Outreach agent at Thu 09:15" in text
    assert "**Why now** (research agent): 60 new franchise locations" in text


def test_run_now_sends_an_approved_but_blocked_email(isolated_state):
    desk = world_until(isolated_state, at(1, 10), approve=False)
    desk.system().kill_switch.engage("Checking")
    desk.world.submit(approval_event(MAYA_EMAIL, True, "you@cool.example"), "Approved while paused")
    desk.step()
    desk.system().kill_switch.release()

    test = page("inbox")
    button(test, "Run now").click().run()
    desk.step()
    assert desk.system().crm.get_deal("D-001").stage.value == "outreach"


# --- Team and trace ----------------------------------------------------------------------------


def test_team_page_is_generated_from_the_code(isolated_state):
    test = page("team")
    text = texts(test)

    assert not test.exception
    assert ":material/smart_toy: **Reply agent**" in text
    assert "`book_meeting` &nbsp; :green-badge[on its own]" in text
    assert "asks you for top tiers (agency: Tier A/B, brand: Tier A) and anything about price" in text
    rules = test.dataframe[0].value
    assert {"first_touch_tier", "high_risk_action", "kill_switch"} <= set(rules["Rule"])


def test_trace_shows_each_tool_call_and_the_rule_behind_it(isolated_state):
    world_until(isolated_state, at(1, 10), approve=False)
    test = page("trace")
    text = texts(test)

    assert not test.exception
    assert test.selectbox[0].value == "lead:L-001"
    assert "**Research agent** · Research BrightBite Burgers" in text
    assert "rule `first_touch_tier`" in text
    assert "The agent's instructions and task (what the model was given)" in [e.label for e in test.expander]


def test_empty_trace_page_explains_itself():
    test = page("trace")
    assert not test.exception
    assert test.info[0].value.startswith("No trace yet")


# --- Every page, empty and after the whole story --------------------------------------------------


@pytest.mark.parametrize("name", ["today", "inbox", "pipeline", "deal", "team", "trace"])
def test_every_page_renders_with_no_data(name):
    assert not page(name).exception


def test_every_page_renders_after_the_whole_story(isolated_state):
    world_until(isolated_state, at(13, 0))
    for name in ("today", "inbox", "pipeline", "deal", "team", "trace"):
        test = page(name)
        assert not test.exception, name


def test_the_deal_page_shows_research_meeting_and_call_brief(isolated_state):
    world_until(isolated_state, at(13, 0))
    test = page("deal")
    text = texts(test)

    assert test.title[0].value == "BrightBite Burgers"
    assert "**Why now** (research agent): 60 new franchise locations" in text
    assert "Meeting with BrightBite Burgers booked for Mon 05 Oct 15:00 UTC" in text
    assert any("Call brief: BrightBite Burgers" in m.value for m in test.markdown)


# --- Sidebar ---------------------------------------------------------------------------------------


def test_choosing_claude_in_the_sidebar_sets_the_world_model(isolated_state):
    world_until(isolated_state, at(1, 8, 45))
    test = app()
    model = next(control for control in test.sidebar.segmented_control if control.label == "Model")
    model.set_value("claude").run()  # the user clicks "claude"

    assert World(isolated_state).read().model == "claude"
    assert test.session_state["saved_llm_mode"] == "claude"


def test_a_new_browser_session_shows_the_model_the_world_runs(isolated_state):
    """Found in use: claude chosen earlier, a new tab showed "scripted" while the world kept calling Claude."""
    Desk(isolated_state).start(model="claude")
    test = app()

    assert test.session_state["llm_mode"] == "claude"
    assert any("Live Claude" in item.value for item in test.sidebar.markdown)


def test_sidebar_shows_paused_agents(isolated_state):
    world_until(isolated_state, at(1, 8, 45)).system().kill_switch.engage("Bad emails")
    test = app()
    assert any("Agents paused: Bad emails" in e.value for e in test.sidebar.error)


def test_sidebar_choices_come_back_if_streamlit_drops_them(monkeypatch):
    """Streamlit can drop a widget's value on a page change; the saved copy brings it back,
    while a value that is still there (a choice just made) is left alone."""
    from revenue_agents.ui import context

    state = {"saved_llm_mode": "claude", "saved_segment": "agency", "segment": "brand"}  # llm_mode was dropped
    monkeypatch.setattr(context.st, "session_state", state)
    context.init_session()

    assert state["llm_mode"] == "claude"  # restored from the saved copy
    assert state["segment"] == "brand"  # kept: the user just chose it
    assert state["reviewer"] == "you@cool.example"  # first visit: default


def test_the_playbook_shows_what_was_learned_where_from_and_where_it_was_used(isolated_state):
    world_until(isolated_state, at(13, 0))
    test = page("playbook")
    text = texts(test)

    assert not test.exception
    assert "**Franchise brands care more about brand control than speed: lead with locked fields and approval " \
           "workflow.**" in text
    assert "From the intro call, Maya Thompson at 00:01:28: “Brand consistency matters more to me than speed.”" in text
    assert ":violet-badge[used in call brief for BrightBite Burgers, email to Crumb & Co Bakeries]" in text
    assert "email to Northwind Media Partners" in text  # a global lesson crossed into the agency segment
    assert "Brands (franchise and multi-location)" in text and "First contact needs you for Tier A/B" in text
    assert "6 lessons learned" in texts(page("today"))  # Today keeps a one-line summary

    deal = AppTest.from_file(HARNESS, default_timeout=30)
    deal.session_state["page"], deal.session_state["selected_deal"] = "deal", "D-007"  # Crumb & Co's email
    deal.run()
    assert "Shaped by what the system learned" in texts(deal)  # the sent email names the lesson, in words
    assert "Franchise brands care more about brand control than speed" in texts(deal)


def test_the_deal_room_tells_the_story_and_the_plan(isolated_state):
    world_until(isolated_state, at(13, 0))
    test = page("deal")
    text = texts(test)

    assert not test.exception
    assert "The story so far" in text and "The plan from here" in text
    assert "Deal fields, with where each came from" in text
    assert "Ken, discovery call 00:01:30" in text
    assert "Closed won and handed off to delivery. Nothing left to do here." in text


def test_coaching_that_isn_t_a_lesson_yet_can_be_kept_or_left(isolated_state):
    desk = world_until(isolated_state, at(1, 10), approve=False)
    desk.world.submit(approval_event(MAYA_EMAIL, False, "you@cool.example", "Mention their Q1 menu launch"), "Coach")
    desk.step()
    test = page("playbook")
    assert "Your coaching, not yet a lesson" in texts(test)
    assert "**“Mention their Q1 menu launch”**" in texts(test)

    button(test, "Keep for brand first emails").click().run()
    desk.step()
    system = desk.system()
    [lesson] = system.lessons.all()
    assert (lesson.source, lesson.text, lesson.said_by) == ("review", "Mention their Q1 menu launch", "you@cool.example")
    assert system.orchestrator.open_coaching() == []
    assert "Your coaching, not yet a lesson" not in texts(page("playbook"))


def test_the_ask_page_runs_a_goal_and_shows_each_step(isolated_state):
    desk = world_until(isolated_state, at(9, 0))
    test = page("ask")
    button(test, "Which deals went quiet after our first email? Follow up").click().run()
    button(test, "Run").click().run()
    desk.step()
    text = texts(page("ask"))

    assert "What the copilot did" in text
    assert "<code>search_deals(stage=outreach, quiet_for_days=2)</code>" in text
    assert "<b>Set a follow-up</b>" in text and "<b>Leave a note for the rep</b>" in text
    assert "Lumen Media Group: follow-up set for Sun 11 Oct and flagged for the rep." in text
