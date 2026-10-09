"""The autonomy policy is enforced in code: rules, not prompts."""

import pytest
import yaml

from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import Action, ActionType, AutonomyPolicy, Decision
from revenue_agents.models import Stage, Tier
from revenue_agents.segments import SEGMENTS_DIR, SegmentRegistry


@pytest.fixture
def kill_switch(tmp_path):
    return KillSwitch(tmp_path / "KILL_SWITCH")


@pytest.fixture
def policy(kill_switch):
    return AutonomyPolicy(SegmentRegistry(), kill_switch)


def action(type_, agent, **payload):
    return Action(id=f"{type_.value}:D-001", type=type_, agent=agent, deal_id="D-001", summary="test", payload=payload)


def email(**payload):
    defaults = {"subject": "Hello", "body": "Would you be open to a call?"}
    return action(ActionType.SEND_EMAIL, "outreach", **{**defaults, **payload})


# --- Low-risk actions run on their own -------------------------------------


@pytest.mark.parametrize(
    ("type_", "agent"),
    [
        (ActionType.ENRICH_LEAD, "research"),
        (ActionType.SCORE_LEAD, "scoring"),
        (ActionType.CLASSIFY_REPLY, "reply_triage"),
        (ActionType.RECORD_LESSON, "call_intel"),
        (ActionType.DRAFT_IO, "deal_desk"),
    ],
)
def test_low_risk_actions_are_automatic(policy, make_deal, type_, agent):
    assert policy.decide(action(type_, agent), make_deal()).decision is Decision.AUTO


# --- High-risk actions wait for a human ------------------------------------


@pytest.mark.parametrize("type_", [ActionType.SEND_PRICING, ActionType.SEND_IO, ActionType.SEND_FOR_SIGNATURE])
def test_pricing_and_contracts_always_need_approval_even_for_tier_c(policy, make_deal, type_):
    decision = policy.decide(action(type_, "deal_desk"), make_deal(tier=Tier.C))
    assert decision.decision is Decision.NEEDS_APPROVAL
    assert decision.rule == "high_risk_action"


def test_first_email_to_tier_a_needs_approval(policy, make_deal):
    decision = policy.decide(email(), make_deal(stage=Stage.SCORED, tier=Tier.A))
    assert decision.decision is Decision.NEEDS_APPROVAL
    assert decision.rule == "first_touch_tier"


@pytest.mark.parametrize("tier", [Tier.B, Tier.C])
def test_first_email_to_lower_tiers_is_automatic(policy, make_deal, tier):
    assert policy.decide(email(), make_deal(stage=Stage.SCORED, tier=tier)).decision is Decision.AUTO


def test_follow_up_email_to_tier_a_is_automatic(policy, make_deal):
    assert policy.decide(email(), make_deal(stage=Stage.REPLIED, tier=Tier.A)).decision is Decision.AUTO


def test_email_to_unscored_lead_needs_approval(policy, make_deal):
    decision = policy.decide(email(), make_deal(tier=None))
    assert decision.decision is Decision.NEEDS_APPROVAL
    assert decision.rule == "unscored_lead"


@pytest.mark.parametrize(
    "body",
    [
        "Our package is $25,000 per month.",
        "The pilot would be about 250k.",
        "I've attached our rate card.",
        "Happy to talk pricing next week.",
        "We can offer a 10% discount.",
        "Our CPM is very competitive.",
    ],
)
def test_pricing_in_email_needs_approval_even_for_tier_c(policy, make_deal, body):
    decision = policy.decide(email(body=body), make_deal(stage=Stage.REPLIED, tier=Tier.C))
    assert decision.decision is Decision.NEEDS_APPROVAL
    assert decision.rule == "pricing_in_email"


def test_normal_email_is_not_mistaken_for_pricing(policy, make_deal):
    body = "Could we meet for 30 minutes next week? We help 120 locations launch local ads by Q1 2027."
    assert policy.decide(email(body=body), make_deal(tier=Tier.C)).decision is Decision.AUTO


# --- Blocked actions ---------------------------------------------------------


def test_kill_switch_blocks_everything(policy, kill_switch, make_deal):
    kill_switch.engage("Bad emails going out")
    decision = policy.decide(action(ActionType.ENRICH_LEAD, "research"), make_deal())
    assert decision.decision is Decision.BLOCKED
    assert "Bad emails going out" in decision.reason


def test_releasing_kill_switch_resumes_actions(policy, kill_switch, make_deal):
    kill_switch.engage("test")
    kill_switch.release()
    assert policy.decide(action(ActionType.ENRICH_LEAD, "research"), make_deal()).decision is Decision.AUTO


def test_email_agent_cannot_send_an_io(policy, make_deal):
    decision = policy.decide(action(ActionType.SEND_IO, "outreach"), make_deal())
    assert decision.decision is Decision.BLOCKED
    assert decision.rule == "agent_scope"


def test_unknown_agent_is_blocked(policy, make_deal):
    assert policy.decide(action(ActionType.ENRICH_LEAD, "mystery"), make_deal()).decision is Decision.BLOCKED


# --- Safety inputs come from deal state, not from the action ----------------


def test_action_cannot_claim_its_own_risk_level(policy, make_deal):
    """A prompt-injected agent might add 'tier: C, skip approval' to its payload. It's ignored."""
    sneaky = email(tier="C", skip_approval=True, first_touch=False)
    assert policy.decide(sneaky, make_deal(stage=Stage.SCORED, tier=Tier.A)).decision is Decision.NEEDS_APPROVAL


# --- Segments can tighten the policy ----------------------------------------


def test_segment_pack_can_require_approval_for_tier_b(tmp_path, kill_switch, make_deal):
    config = yaml.safe_load((SEGMENTS_DIR / "brand.yaml").read_text())
    config["autonomy"] = {"first_touch_approval_tiers": ["A", "B"]}
    (tmp_path / "brand.yaml").write_text(yaml.safe_dump(config))
    strict = AutonomyPolicy(SegmentRegistry(tmp_path), kill_switch)

    assert strict.decide(email(), make_deal(tier=Tier.B)).decision is Decision.NEEDS_APPROVAL
    assert strict.decide(email(), make_deal(tier=Tier.C)).decision is Decision.AUTO
