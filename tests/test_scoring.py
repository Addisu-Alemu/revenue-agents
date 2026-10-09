"""Scoring is deterministic, explained, and driven entirely by the segment pack."""

import pytest

from revenue_agents.models import Engagement, Tier
from revenue_agents.segments import FitRule, TierThresholds
from revenue_agents.skills.scoring import rule_matches, score_engagement, score_fit, score_lead, tier_for


@pytest.mark.parametrize(
    ("lead_id", "total", "tier"),
    [
        ("L-001", 97.0, Tier.A),  # BrightBite: big franchise, strong signals, engaged
        ("L-002", 32.0, Tier.B),  # Harbor Lane: multi-location but small, one signal
        ("L-003", 0.0, Tier.C),  # Pebble & Pine: single shop, no signals, no engagement
    ],
)
def test_brand_leads_get_expected_tier(leads, enricher, brand_pack, lead_id, total, tier):
    lead = leads[lead_id]
    score = score_lead(lead, enricher.enrich(lead), brand_pack.scoring)
    assert score.total == total
    assert score.tier is tier


def test_every_sub_score_has_a_reason(leads, enricher, brand_pack):
    lead = leads["L-002"]
    score = score_lead(lead, enricher.enrich(lead), brand_pack.scoring)
    assert "Franchise or multi-location business (+30)" in score.fit.reason
    assert "Missed: 50+ locations" in score.fit.reason
    assert "holiday season promotion" in score.intent.reason
    assert "2 website visits (+10)" in score.engagement.reason


def test_empty_lead_explains_why_it_scored_zero(leads, enricher, brand_pack):
    lead = leads["L-003"]
    score = score_lead(lead, enricher.enrich(lead), brand_pack.scoring)
    assert score.fit.reason.startswith("No ICP criteria matched")
    assert score.intent.reason == "No buying signals found."
    assert score.engagement.reason == "No engagement yet."


def test_weights_come_from_config(leads, enricher, brand_pack):
    fit_only = brand_pack.scoring.model_copy(
        update={"weights": brand_pack.scoring.weights.model_copy(update={"fit": 1.0, "intent": 0.0, "engagement": 0.0})}
    )
    lead = leads["L-002"]
    assert score_lead(lead, enricher.enrich(lead), fit_only).total == 40.0


def test_sub_scores_are_capped_at_100():
    score = score_engagement(Engagement(email_opens=50), {"email_opens": 5})
    assert score.value == 100
    assert "Capped at 100, raw 250" in score.reason


def test_fit_is_capped_at_100(leads, enricher):
    rules = [
        FitRule(field="locations", gte=1, points=60, reason="Has locations"),
        FitRule(field="employees", gte=1, points=60, reason="Has employees"),
    ]
    assert score_fit(enricher.enrich(leads["L-001"]), rules).value == 100


@pytest.mark.parametrize(("total", "tier"), [(70, Tier.A), (69.9, Tier.B), (30, Tier.B), (29.9, Tier.C)])
def test_tier_boundaries(total, tier):
    assert tier_for(total, TierThresholds(A=70, B=30)) is tier


@pytest.mark.parametrize(
    ("rule", "value", "expected"),
    [
        (FitRule(field="locations", gte=50, points=1, reason="r"), 50, True),
        (FitRule(field="locations", gte=50, points=1, reason="r"), 49, False),
        (FitRule(field="locations", gte=50, points=1, reason="r"), None, False),
        (FitRule(field="business_model", one_of=["Franchise"], points=1, reason="r"), "franchise", True),
        (FitRule(field="tech_stack", contains_any=["DV360"], points=1, reason="r"), ["Meta Ads", "DV360"], True),
        (FitRule(field="tech_stack", contains_any=["DV360"], points=1, reason="r"), [], False),
        (FitRule(field="industry", contains_any=["media"], points=1, reason="r"), "Media agency", True),
    ],
)
def test_rule_checks(rule, value, expected):
    assert rule_matches(rule, value) is expected
