"""The agency segment works from its config pack alone (segments/agency.yaml)."""

import pytest

from revenue_agents.demo import run
from revenue_agents.demo_llm import demo_llm
from revenue_agents.events import Event, EventType
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import Action, ActionType, AutonomyPolicy, Decision
from revenue_agents.models import Stage, Tier
from revenue_agents.skills.scoring import score_lead
from revenue_agents.runtime import ScriptedRuntime
from revenue_agents.system import build_system


@pytest.fixture
def agency(segments):
    return segments.get("agency")


def test_both_packs_are_available(segments):
    assert segments.names() == ["agency", "brand"]


@pytest.mark.parametrize(
    ("lead_id", "total", "tier"),
    [
        ("L-004", 86.2, Tier.A),  # Lumen: 55 clients, $38M spend, client win
        ("L-005", 35.0, Tier.B),  # Copperline: 14 clients, $3M spend
        ("L-006", 10.0, Tier.C),  # Solo Studio: 3 people
    ],
)
def test_agency_leads_are_scored_by_agency_rules(leads, enricher, agency, lead_id, total, tier):
    lead = leads[lead_id]
    score = score_lead(lead, enricher.enrich(lead), agency.scoring)
    assert (score.total, score.tier) == (total, tier)


def test_agency_rules_use_fields_brands_do_not(leads, enricher, agency):
    lead = leads["L-004"]
    reason = score_lead(lead, enricher.enrich(lead), agency.scoring).fit.reason
    assert "Manages 20+ client accounts (+25)" in reason
    assert "Capped at 100, raw 125" in reason


def test_agencies_need_approval_for_tier_b_first_touch(tmp_path, segments, make_deal):
    policy = AutonomyPolicy(segments, KillSwitch(tmp_path / "KILL_SWITCH"))
    email = Action(id="send_email:D-005:first_touch", type=ActionType.SEND_EMAIL, agent="outreach",
                   deal_id="D-005", summary="First email", payload={"subject": "Hi", "body": "Quick question?"})

    agency_deal = make_deal(stage=Stage.SCORED, tier=Tier.B, segment="agency")
    brand_deal = make_deal(stage=Stage.SCORED, tier=Tier.B, segment="brand")

    assert policy.decide(email, agency_deal).decision is Decision.NEEDS_APPROVAL
    assert policy.decide(email, brand_deal).decision is Decision.AUTO


def test_agency_demo_runs_end_to_end(tmp_path):
    lines = []
    code = run(segment="agency", state_dir=tmp_path / "demo", auto_approve=True, out=lines.append)
    output = "\n".join(lines)

    assert code == 0
    assert "Segment pack: Agencies (media, creative and programmatic)" in output
    assert "Subject: Creative at scale across Lumen Media Group's clients" in output
    assert "queued (First contact with a Tier B lead)" in output  # Copperline: agency rule
    assert "Tier C: no outreach, left for nurture" in output  # Solo Studio
    assert "Flags: objection, pricing_requested" in output  # Sam's reply


def test_a_global_lesson_from_a_brand_call_shapes_agency_outreach(tmp_path, transcript):
    system = build_system(ScriptedRuntime(demo_llm()), state_dir=tmp_path)
    system.orchestrator.handle(Event(id="lead:L-001", type=EventType.LEAD_CREATED, payload={"lead_id": "L-001"}))
    system.orchestrator.handle(Event(id="call:1", type=EventType.CALL_COMPLETED, payload={
        "lead_id": "L-001", "transcript_id": "L-001_discovery_call", "transcript": transcript}))

    system.orchestrator.handle(Event(id="lead:L-004", type=EventType.LEAD_CREATED, payload={"lead_id": "L-004"}))

    [email] = [r.action for r in system.approvals.pending() if r.action.deal_id == "D-004"]
    [used] = email.payload["lessons_used"]
    lesson = next(lesson for lesson in system.lessons.all() if lesson.id == used)
    assert (lesson.segment, lesson.scope) == ("brand", "global")  # learned on a brand call, used for an agency
    assert "lesson:L-001_discovery_call:0" not in email.payload["lessons_used"]  # franchise-only lesson stays out
