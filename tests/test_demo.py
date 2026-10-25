"""The terminal demo runs the live world end to end in scripted mode, pauses for approvals, and fails clearly."""

import pytest

from revenue_agents.demo import run
from revenue_agents.runtime import ClaudeRuntime, ScriptedRuntime
from revenue_agents.system import make_runtime


def demo(tmp_path, decide=None, **kwargs):
    """Run the demo. `decide(approval_line)` answers each approval prompt; other prompts get an empty answer."""
    lines = []

    def ask(prompt):
        if "Approve?" not in prompt or decide is None:
            return ""
        return decide(next(line for line in reversed(lines) if "APPROVAL NEEDED" in line))

    code = run(state_dir=tmp_path / "demo", out=lines.append, ask=ask, **kwargs)
    return code, "\n".join(lines)


def test_demo_runs_the_live_world_end_to_end(tmp_path):
    code, output = demo(tmp_path, auto_approve=True)

    assert code == 0
    assert "NOTE: scripted mode" in output
    assert "APPROVAL NEEDED: First email to Maya Thompson" in output
    assert "Meeting with BrightBite Burgers booked for Thu 08 Oct 15:00 UTC" in output  # the reply agent booked it
    assert "Already handled: ignored" in output  # Maya's webhook delivered twice
    assert "Stage proposal -> signature" in output
    assert "Rejected: unknown envelope 'ENV-FAKE-001'" in output
    assert "Stage signature -> handoff" in output
    assert "Handoff to delivery:" in output
    assert "Suspicious content: ignored as instructions" in output
    assert "Tier C: no outreach, left for nurture" in output
    assert "approvals pending: 0" in output


def test_later_emails_apply_what_earlier_calls_taught(tmp_path):
    _, output = demo(tmp_path, auto_approve=True)

    assert "Draft uses lessons: lesson:L-001_intro_call:0" in output  # Crumb & Co (brand), sent on its own
    assert "Lessons used: lesson:L-001_discovery_call:g0" in output  # Northwind (agency): a global lesson


def test_agents_set_timers_that_the_world_keeps(tmp_path):
    _, output = demo(tmp_path, auto_approve=True)

    assert "Follow up with Owen Castillo on Wed 06 Jan 2027" in output
    assert "Coming up (timers the agents set)" in output
    assert "Tue 05 Jan 2027: Follow up with Sam Whitfield" in output
    assert "The last daily digest" in output


def test_rejecting_maya_s_email_means_she_never_replies(tmp_path):
    code, output = demo(tmp_path, decide=lambda line: "n" if "Maya Thompson" in line else "y")

    assert code == 0
    assert "Rejected by demo-reviewer@cool.example: send_email:D-001:first_touch" in output
    assert "Maya replies" not in output  # the simulator only answers an email that went out
    assert "D-001  BrightBite Burgers        A       97  scored" in output


def test_rejecting_the_io_means_nothing_is_signed(tmp_path):
    code, output = demo(tmp_path, decide=lambda line: "n" if "Send IO-D-001 " in line else "y")

    assert code == 0
    assert "Rejected by demo-reviewer@cool.example: send_io:D-001:IO-D-001" in output
    assert "signs the IO" not in output
    assert "D-001  BrightBite Burgers        A       97  proposal" in output


def test_demo_starts_fresh_each_time(tmp_path):
    demo(tmp_path, auto_approve=True)
    code, output = demo(tmp_path, auto_approve=True)
    assert code == 0
    assert "New lead: BrightBite Burgers (brand) -> deal D-001" in output  # not "duplicate"


def test_one_segment_can_be_shown_on_its_own(tmp_path):
    code, output = demo(tmp_path, segment="agency", auto_approve=True)

    assert code == 0
    assert "Segment pack: Agencies" in output
    assert "New lead: Lumen Media Group" in output
    assert "New lead: BrightBite Burgers" not in output


def test_unknown_segment_fails_with_a_clear_message(tmp_path):
    code, output = demo(tmp_path, segment="publisher", auto_approve=True)
    assert code == 1
    assert "no config pack for segment 'publisher'. Available: agency, brand" in output


def test_claude_mode_without_key_fails_instead_of_faking(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, output = demo(tmp_path, llm_mode="claude", auto_approve=True)
    assert code == 1
    assert "FAILED:" in output and "ANTHROPIC_API_KEY is not set" in output
    assert "NOTE: scripted mode" not in output


def test_make_runtime_modes():
    assert isinstance(make_runtime("scripted"), ScriptedRuntime)
    assert isinstance(make_runtime("claude"), ClaudeRuntime)
    with pytest.raises(ValueError, match="use 'scripted' or 'claude'"):
        make_runtime("gpt")
