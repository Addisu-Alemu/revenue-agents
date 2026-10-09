"""The mock data must always fit the models, and bad data must be rejected."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from revenue_agents.models import Deal, Engagement, Enrichment, Lead, LeadScore, Reply, Stage

DATA = Path(__file__).resolve().parent.parent / "data"


def load(name):
    return json.loads((DATA / name).read_text())


def test_all_leads_load():
    leads = [Lead.model_validate(item) for item in load("leads.json")]
    assert len(leads) == 6
    assert {lead.segment for lead in leads} == {"brand", "agency"}


def test_every_lead_has_enrichment():
    enrichment = {domain: Enrichment.model_validate(item) for domain, item in load("enrichment.json").items()}
    for lead in load("leads.json"):
        assert lead["domain"] in enrichment


def test_replies_load_with_from_field():
    replies = [Reply.model_validate(item) for item in load("replies.json")]
    assert replies[0].sender == "maya.thompson@brightbite.example"


def test_negative_engagement_is_rejected():
    with pytest.raises(ValidationError):
        Engagement(email_opens=-1)


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Engagement(email_open=3)  # typo: missing "s"


def test_unknown_tier_is_rejected():
    sub = {"value": 50, "reason": "test"}
    with pytest.raises(ValidationError):
        LeadScore(fit=sub, intent=sub, engagement=sub, total=50, tier="Z")


def test_new_deal_starts_at_prospect():
    deal = Deal(id="D-001", lead_id="L-001", segment="brand")
    assert deal.stage is Stage.PROSPECT
    assert deal.flags == []
