"""The agency segment works from its config pack alone (segments/agency.yaml)."""

import pytest

from revenue_agents.demo import run
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import Action, ActionType, AutonomyPolicy, Decision
from revenue_agents.models import Stage, Tier
from revenue_agents.skills.scoring import score_lead


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
