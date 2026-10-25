"""The UI shows the live world and sends every decision through it. No browser needed (Streamlit AppTest).

The background runner is off in these tests (REVENUE_AGENTS_RUNNER=off): each test moves the world itself, so
what a page shows and what a click does can be checked one step at a time.
"""

import datetime as dt
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from revenue_agents.events import approval_event
from revenue_agents.storage import set_clock
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
    assert test.subheader[0].value.startswith("1 decision needs you · 3 actions the agents took on their own")
    assert "**First email to Maya Thompson at BrightBite Burgers**" in text
    assert "**Research agent** · Research BrightBite Burgers" in text
    assert ":orange-badge[send_email · asks you]" in text
    assert "Thu 01 Oct 2026 · 10:00 UTC" in text


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
    assert ":orange-badge[waits: agents paused]" in texts(test)

    button(test, "Release agents").click().run()
    released = desk.step()
    assert released.label == "You released the agents"
    assert "New lead: BrightBite Burgers (brand) -> deal D-001" in " ".join(released.steps)


# --- Inbox --------------------------------------------------------------------------------------


def test_approving_in_the_inbox_goes_through_the_desk(isolated_state):
    desk = world_until(isolated_state, at(1, 10), approve=False)
    test = page("inbox")
    assert "Why it stopped:" in test.warning[0].value

    button(test, "Approve").click().run()
    assert not test.exception
    [item] = [item for item in desk.world.read().queue if item.source == "you"]
    assert item.label == "Approved by you: First email to Maya Thompson at BrightBite Burgers"

    desk.step()
    system = desk.system()
    assert system.approvals.get(MAYA_EMAIL).status.value == "approved"
    assert [a.kind for a in system.crm.timeline("D-001")][-2:] == ["email_sent", "stage_change"]


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

    assert "**What the outreach agent did** · First email to Maya Thompson at BrightBite Burgers" in text
    assert ":gray-badge[read_account] :gray-badge[search_lessons] :gray-badge[draft_email]" in text
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
    assert "Meeting with BrightBite Burgers booked for Thu 08 Oct 15:00 UTC" in text
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


def test_the_knowledge_loop_shows_what_was_learned_and_where_it_was_used(isolated_state):
    world_until(isolated_state, at(13, 0))
    test = page("today")
    text = texts(test)

    assert any(e.label.startswith("Knowledge loop:") for e in test.expander)
    assert "Learned from call L-001_intro_call · brand only" in text
    assert "Used in: call brief for BrightBite Burgers, email to Crumb & Co Bakeries" in text
    assert "email to Northwind Media Partners" in text  # a global lesson crossed into the agency segment

    deal = AppTest.from_file(HARNESS, default_timeout=30)
    deal.session_state["page"], deal.session_state["selected_deal"] = "deal", "D-007"  # Crumb & Co's email
    deal.run()
    assert "Shaped by what the system learned" in texts(deal)  # the sent email names the lesson, in words
    assert "Franchise brands care more about brand control than speed" in texts(deal)
