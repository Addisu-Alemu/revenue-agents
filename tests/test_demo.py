"""The live demo runs end to end in fake mode, pauses for approvals, and fails clearly."""

import pytest

from revenue_agents.demo import run
from revenue_agents.llm import ClaudeLLM, FakeLLM
from revenue_agents.system import make_llm


def demo(tmp_path, **kwargs):
    lines = []
    code = run(state_dir=tmp_path / "demo", out=lines.append, **kwargs)
    return code, "\n".join(lines)


def test_brand_demo_runs_end_to_end(tmp_path):
    code, output = demo(tmp_path, auto_approve=True)

    assert code == 0
    assert "NOTE: fake mode" in output
    assert "APPROVAL NEEDED: First email to Maya Thompson" in output
    assert "Already handled: ignored" in output  # duplicate webhook
    assert "Stage proposal -> signature" in output
    assert "Rejected: unknown envelope 'ENV-FAKE-001'" in output
    assert "Stage signature -> handoff" in output
    assert "Handoff to delivery:" in output
    assert "Lessons used: lesson:L-001_discovery_call:0" in output
    assert "Suspicious content: ignored as instructions" in output
    assert "Tier C: no outreach, left for nurture" in output
    assert "used in: D-002" in output
    assert "6 days later" in output
    assert "Follow up with Owen Castillo: no progress in replied" in output
    assert "Daily digest" in output
    assert "approvals pending: 0" in output


def test_rejecting_in_the_prompt_sends_nothing(tmp_path):
    answers = iter(["n", ""] + ["y"] * 10)  # reject Maya's email, no feedback: no redraft
    code, output = demo(tmp_path, ask=lambda prompt: next(answers))

    assert code == 0
    assert "Rejected by demo-reviewer@cool.example: send_email:D-001:first_touch" in output
    # BrightBite never reaches "outreach": no email was sent (the scripted reply still arrives later)
    assert "Stage scored -> replied" in output
    assert "Stage scored -> outreach\n" not in output.split("=== 2.")[0]


def test_demo_starts_fresh_each_time(tmp_path):
    demo(tmp_path, auto_approve=True)
    code, output = demo(tmp_path, auto_approve=True)
    assert code == 0
    assert "New lead: BrightBite Burgers (brand) -> deal D-001" in output  # not "duplicate"


def test_unknown_segment_fails_with_a_clear_message(tmp_path):
    code, output = demo(tmp_path, segment="publisher", auto_approve=True)
    assert code == 1
    assert "no config pack for segment 'publisher'. Available: agency, brand" in output


def test_claude_mode_without_key_fails_instead_of_faking(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, output = demo(tmp_path, llm_mode="claude", auto_approve=True)
    assert code == 1
    assert "FAILED: ANTHROPIC_API_KEY is not set" in output
    assert "NOTE: fake mode" not in output


def test_make_llm_modes():
    assert isinstance(make_llm("fake"), FakeLLM)
    assert isinstance(make_llm("claude"), ClaudeLLM)
    with pytest.raises(ValueError, match="use 'fake' or 'claude'"):
        make_llm("gpt")


def test_rejecting_the_io_means_nothing_to_sign(tmp_path):
    answers = iter(["y", "n"] + ["y"] * 10)  # approve Maya's email, reject the IO
    code, output = demo(tmp_path, ask=lambda prompt: next(answers))

    assert code == 0
    assert "Rejected by demo-reviewer@cool.example: send_io:D-001:IO-D-001" in output
    assert "No IO was sent, so there is nothing to sign." in output
    assert "D-001  BrightBite Burgers     A       97  proposal" in output
