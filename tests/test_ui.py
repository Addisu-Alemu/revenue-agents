"""The UI renders every page and its buttons go through the orchestrator. No browser needed (Streamlit AppTest)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from revenue_agents import story
from revenue_agents.demo import run
from revenue_agents.system import build_system, make_llm

ROOT = Path(__file__).resolve().parent.parent
HARNESS = str(ROOT / "tests" / "ui_harness.py")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("REVENUE_AGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "state"


def page(name):
    at = AppTest.from_file(HARNESS, default_timeout=30)
    at.session_state["page"] = name
    return at.run()


def texts(at):
    return "\n".join([m.value for m in at.markdown] + [c.value for c in at.caption] + [t.value for t in at.title])


def button(at, label):
    return next(b for b in at.button if b.label.startswith(label))


def full_story(state_dir):
    run(state_dir=state_dir, auto_approve=True, fresh=True, out=lambda line: None)


# --- Main app --------------------------------------------------------------------------


def test_app_runs_a_step_and_an_approval():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Live demo"

    button(at, "Next: New lead: BrightBite").click().run()
    assert "1 high-risk action(s) waiting for a human" in at.info[0].value
    assert button(at, "Next:").disabled  # can't continue while a human decision is pending

    button(at, "Approve").click().run()
    assert not at.exception
    assert "**Approved: First email to Maya Thompson at BrightBite Burgers**" in texts(at)
    assert not button(at, "Next:").disabled


def test_sidebar_shows_the_kill_switch(isolated_state):
    build_system(make_llm("fake"), state_dir=isolated_state).kill_switch.engage("Bad emails")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert any("Kill switch ON: Bad emails" in e.value for e in at.sidebar.error)


# --- Pages, empty and full ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["demo", "pipeline", "deal", "approvals"])
def test_every_page_renders_with_no_data(name):
    assert not page(name).exception


def test_pages_show_the_finished_story(isolated_state):
    full_story(isolated_state)

    pipeline = page("pipeline")
    assert not pipeline.exception
    assert "**BrightBite Burgers**" in texts(pipeline)

    deal = page("deal")
    assert not deal.exception
    assert deal.title[0].value == "BrightBite Burgers"
    assert "Why this tier" in [h.value for h in deal.subheader]

    approvals = page("approvals")
    assert "Decision history" in [h.value for h in approvals.subheader]
    assert approvals.success[0].value.startswith("Nothing waiting")


def test_story_is_complete_after_a_full_run(isolated_state):
    full_story(isolated_state)
    assert button(page("demo"), "Story complete").disabled


def test_rejecting_the_io_skips_the_signature_step_instead_of_getting_stuck(isolated_state):
    answers = iter(["y", "n"] + ["y"] * 10)  # approve the email, reject the IO
    run(state_dir=isolated_state, ask=lambda prompt: next(answers), out=lambda line: None)
    assert button(page("demo"), "Story complete").disabled


def test_rejecting_from_the_approvals_page(isolated_state):
    at = page("demo")
    button(at, "Next: New lead: BrightBite").click().run()

    queue = page("approvals")
    button(queue, "Reject").click().run()
    assert not queue.exception
    system = build_system(make_llm("fake"), state_dir=isolated_state)
    assert system.approvals.pending() == []
    assert system.approvals.all()[0].status.value == "rejected"


def test_the_whole_live_demo_can_be_clicked_through(isolated_state):
    """Press Next and Approve until the story is complete, like the live demo."""
    at = page("demo")
    for _ in range(40):
        approve = [b for b in at.button if b.label == "Approve"]
        if approve:
            approve[0].click().run()
            continue
        nxt = button(at, "Next:") if any(b.label.startswith("Next:") for b in at.button) else None
        if nxt is None:
            break
        nxt.click().run()
        assert not at.exception

    assert button(at, "Story complete").disabled
    system = build_system(make_llm("fake"), state_dir=isolated_state)
    stages = {deal.id: deal.stage.value for deal in system.crm.deals()}
    assert stages == {"D-001": "handoff", "D-002": "replied", "D-003": "scored"}
    assert "event_duplicate" in [event.event for event in system.audit.events()]  # Maya's webhook, sent twice


def test_reset_asks_first_then_clears_and_confirms(isolated_state):
    full_story(isolated_state)
    at = page("demo")
    assert not any(b.label == "Yes, reset" for b in at.button)  # nothing to confirm until asked

    button(at, "Reset demo").click().run()
    button(at, "Yes, reset").click().run()

    assert not at.exception
    assert build_system(make_llm("fake"), state_dir=isolated_state).crm.deals() == []
    assert at.toast[0].value.startswith("Demo reset")
    assert not any(b.label == "Yes, reset" for b in at.button)  # the dialog closed


def test_cancelling_the_reset_keeps_everything(isolated_state):
    full_story(isolated_state)
    at = page("demo")
    button(at, "Reset demo").click().run()
    button(at, "Cancel").click().run()

    assert len(build_system(make_llm("fake"), state_dir=isolated_state).crm.deals()) == 3


def test_run_now_button_sends_an_approved_but_blocked_email(isolated_state):
    system = build_system(make_llm("fake"), state_dir=isolated_state)
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-001")))
    system.kill_switch.engage("investigating")
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", True, "you@cool.example"))
    system.kill_switch.release()

    queue = page("approvals")
    assert "Approved, not executed yet" in [h.value for h in queue.subheader]
    button(queue, "Run now").click().run()

    assert not queue.exception
    assert system.crm.get_deal("D-001").stage.value == "outreach"


def test_demo_page_explains_a_paused_system(isolated_state):
    build_system(make_llm("fake"), state_dir=isolated_state).orchestrator.pause("Bad emails", by="ops")
    at = page("demo")
    assert any("Kill switch is ON: new events are held" in e.value for e in at.error)


def test_reject_and_redraft_from_the_ui_with_a_lesson(isolated_state):
    at = page("demo")
    button(at, "Next: New lead: BrightBite").click().run()

    at.text_input[0].input("Lead with the operational pain, not congratulations").run()
    at.checkbox[0].check().run()
    button(at, "Reject & redraft").click().run()

    assert not at.exception
    system = build_system(make_llm("fake"), state_dir=isolated_state)
    assert [r.id for r in system.approvals.pending()] == ["send_email:D-001:first_touch:v2"]
    assert system.lessons.all()[0].source == "review"
    assert any("(draft v2, after review)" in m.value for m in at.markdown)
