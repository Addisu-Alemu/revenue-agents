"""The feed is read from the trace: each run with the tools the agent chose, and how each call was decided."""

import datetime as dt

from revenue_agents.storage import set_clock
from revenue_agents.ui import feed
from revenue_agents.world import Desk


def at(day, hour):
    return dt.datetime(2026, 10, day, hour, tzinfo=dt.timezone.utc)


def test_a_new_lead_reads_as_two_agent_runs(tmp_path):
    desk = Desk(tmp_path)
    desk.start()
    desk.run_until(at(1, 10))
    set_clock(None)

    [entry] = [e for e in feed.entries(desk.world, desk.system().tracer.spans()) if e.source == "lead"]
    research, outreach = entry.runs
    assert entry.label == "New lead: BrightBite Burgers"
    assert [step.tool for step in research.tools] == ["read_lead", "enrich_company", "score_lead", "save_research_note"]
    assert research.outcome == "on its own"
    assert [step.tone for step in outreach.tools] == ["read", "read", "read", "asked"]
    assert outreach.outcome == "asked you"


def test_the_trail_behind_an_approval_is_the_run_that_proposed_it(tmp_path):
    desk = Desk(tmp_path)
    desk.start()
    desk.run_until(at(1, 10))
    set_clock(None)
    spans = desk.system().tracer.spans()

    run = feed.trail_for(spans, "send_email:D-001:first_touch")
    assert (run.agent, run.title) == ("outreach", "First email to Maya Thompson at BrightBite Burgers")
    assert feed.trail_for(spans, "send_email:D-999:first_touch") is None


def test_only_side_effects_count_as_acting_on_their_own(tmp_path):
    desk = Desk(tmp_path)
    desk.start()
    desk.run_until(at(1, 10))
    set_clock(None)
    assert feed.acted_on_their_own(desk.system().tracer.spans()) == 3  # enrich, score, note; the email waits


def test_a_draft_code_sent_back_is_marked_to_fix():
    step = feed.ToolStep("draft_email", "allow", None, "read_only", "", "Draft rejected: ...", is_error=True)
    assert step.tone == "fix"
    assert feed.ToolStep("send_email", "deny", None, "tool_rule", "", "").tone == "refused"
    assert feed.ToolStep("send_email", "allow", "blocked", "kill_switch", "", "").tone == "refused"
