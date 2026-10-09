"""Scoring skill: fit, intent and engagement sub-scores, a total and a tier.

Plain Python, no AI. The tier decides whether a human must approve first
contact, so scoring has to be deterministic, testable and explainable. Every
number (points, weights, tier cut-offs) comes from the segment pack.
"""

from __future__ import annotations

from typing import Any

from revenue_agents.models import Engagement, Enrichment, Lead, LeadScore, SubScore, Tier
from revenue_agents.segments import FitRule, ScoringConfig, TierThresholds

MAX_SCORE = 100.0


def score_lead(lead: Lead, enrichment: Enrichment, config: ScoringConfig) -> LeadScore:
    """Score one lead with one segment's scoring config."""
    fit = score_fit(enrichment, config.fit_rules)
    intent = score_intent(enrichment, config.intent_points)
    engagement = score_engagement(lead.engagement, config.engagement_points)

    weights = config.weights
    total = round(fit.value * weights.fit + intent.value * weights.intent + engagement.value * weights.engagement, 1)

    return LeadScore(fit=fit, intent=intent, engagement=engagement, total=total, tier=tier_for(total, config.tiers))


def tier_for(total: float, tiers: TierThresholds) -> Tier:
    if total >= tiers.A:
        return Tier.A
    if total >= tiers.B:
        return Tier.B
    return Tier.C


# --- Sub-scores ------------------------------------------------------------


def score_fit(enrichment: Enrichment, rules: list[FitRule]) -> SubScore:
    matched = [rule for rule in rules if rule_matches(rule, getattr(enrichment, rule.field))]
    missed = [rule for rule in rules if rule not in matched]

    raw = sum(rule.points for rule in matched)
    if matched:
        reason = "Matched: " + "; ".join(f"{rule.reason} (+{rule.points:g})" for rule in matched) + "."
    else:
        reason = "No ICP criteria matched."
    if missed:
        reason += " Missed: " + "; ".join(rule.reason for rule in missed) + "."
    return _sub_score(raw, reason)


def score_intent(enrichment: Enrichment, points: dict[str, float]) -> SubScore:
    hits = [(signal, points[signal.type]) for signal in enrichment.signals if signal.type in points]

    raw = sum(value for _, value in hits)
    if not hits:
        return _sub_score(raw, "No buying signals found.")
    details = [f"{signal.detail} ({signal.type}, +{value:g})" for signal, value in hits]
    return _sub_score(raw, "Signals: " + "; ".join(details) + ".")


def score_engagement(engagement: Engagement, points: dict[str, float]) -> SubScore:
    hits = [(field, count, count * value) for field, value in points.items() if (count := getattr(engagement, field))]

    raw = sum(earned for _, _, earned in hits)
    if not hits:
        return _sub_score(raw, "No engagement yet.")
    details = [f"{count} {field.replace('_', ' ')} (+{earned:g})" for field, count, earned in hits]
    return _sub_score(raw, "; ".join(details) + ".")


# --- Helpers ---------------------------------------------------------------


def rule_matches(rule: FitRule, value: Any) -> bool:
    """Apply a fit rule's single check to one enrichment value."""
    if value is None:
        return False
    if rule.gte is not None:
        return isinstance(value, (int, float)) and value >= rule.gte
    if rule.one_of is not None:
        return str(value).lower() in {option.lower() for option in rule.one_of}

    wanted = [option.lower() for option in rule.contains_any or []]
    if isinstance(value, list):  # e.g. tech_stack: exact item match
        return any(str(item).lower() in wanted for item in value)
    return any(option in str(value).lower() for option in wanted)  # e.g. industry: text contains


def _sub_score(raw: float, reason: str) -> SubScore:
    if raw > MAX_SCORE:
        return SubScore(value=MAX_SCORE, reason=f"{reason} (Capped at {MAX_SCORE:g}, raw {raw:g}.)")
    return SubScore(value=raw, reason=reason)
