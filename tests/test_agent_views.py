"""Live progress, the agent map and scorecards are built from the real trace."""

import pytest
from streamlit.testing.v1 import AppTest

from revenue_agents import story
from revenue_agents.demo_llm import demo_llm
from revenue_agents.system import build_system
from revenue_agents.ui import agent_views


@pytest.fixture
def first_lead_spans(tmp_path):
    system = build_system(demo_llm(), state_dir=tmp_path)
    story.run(system, story.plan(system.crm.leads(), "brand")[0])
    return system.tracer.spans(), len(system.approvals.pending())


@pytest.mark.parametrize(("name", "output", "expected"), [
    ("research", {"locations": 420, "signals": [{"type": "hiring"}]}, "420 locations · hiring"),
    ("scoring", {"total": 97.0, "tier": "A"}, "97.0 · Tier A"),
    ("outreach", {"lessons_used": ["lesson:x"]}, "email drafted · uses 1 lesson(s)"),
    ("reply_triage", {"classification": {"intent": "meeting_request"}, "flags": ["meeting_requested"]},
     "meeting_request · meeting_requested"),
    ("call_brief", {"questions": [1, 2], "lessons": [1]}, "brief ready · 2 questions · 1 lesson(s)"),
    ("call_intel", {"fields": {"budget_usd": 250000, "decision_maker": "Ken"}, "lessons": []},
     "budget 250000 · signer Ken · 0 lesson(s)"),
    ("deal_desk", {"io_number": "IO-D-001", "errors": []}, "IO-D-001 drafted"),
    ("deal_desk", {"errors": ["budget is missing"]}, "blocked: budget is missing"),
    ("unknown", None, "done"),
])
def test_each_agent_has_a_one_line_result(name, output, expected):
    assert agent_views.headline(name, output) == expected


def test_model_calls_are_grouped_with_their_agent(first_lead_spans):
    spans, _ = first_lead_spans
    runs = agent_views.agent_runs(spans)
    assert [(agent.name, len(calls)) for agent, calls in runs] == [("research", 0), ("scoring", 0), ("outreach", 1)]


def test_agent_map_lights_up_only_what_worked(first_lead_spans):
    spans, waiting = first_lead_spans
    dot = agent_views.agent_map(spans, waiting)

    lit = agent_views.ACTIVE
    assert f'research [label="Research\\n(code)\\n420 locations' in dot and f'fillcolor="{lit}"' in dot
    assert 'reply_triage [label="Reply triage\\n(AI)", shape=box, style="rounded"' in dot  # idle
    assert 'Approval queue\\n1 waiting' in dot
    assert dot.startswith("digraph agents {") and dot.rstrip().endswith("}")


def test_empty_map_has_everything_idle():
    dot = agent_views.agent_map([], 0)
    assert "filled" not in dot
    assert "(code, not AI)" in dot


def test_long_results_are_shortened_cleanly():
    assert agent_views._short("420 locations · hiring, expansion, campaign", 30) == "420 locations · hiring, expan…"
    assert agent_views._short("short", 30) == "short"


def live_progress_script():
    from revenue_agents.ui.agent_views import LiveProgress

    live = LiveProgress()
    live("agent_start", "outreach", {"inputs": []}, None)
    live("llm_start", "OutreachDraft", {"model": "claude-opus-5-5"}, None)
    live("llm", "OutreachDraft", {"schema": "OutreachDraft", "model": "claude-opus-5-5", "cost_usd": 0.0123}, 2300)
    live("agent", "outreach", {"output": {"lessons_used": []}, "proposed": []}, 2310)
    live("policy", "send_email", {"decision": "needs_approval", "rule": "first_touch_tier",
                                  "reason": "First contact with a Tier A lead"}, None)
    live("policy", "score_lead", {"decision": "auto", "rule": "low_risk", "reason": "low risk"}, None)
    live("stage", "scored", {"from": "enriched", "to": "scored", "why": "exit criteria met"}, None)


def test_live_progress_turns_working_into_results():
    at = AppTest.from_function(live_progress_script).run()
    text = "\n".join(m.value for m in at.markdown)

    assert "working…" not in text  # replaced by the result
    assert "**Outreach agent** (AI) · email drafted" in text
    assert "model answered" in text and "$0.0123" in text
    assert "Policy: needs approval" in text
    assert "low risk" not in text  # automatic decisions aren't listed
    assert "Stage enriched → scored" in text
