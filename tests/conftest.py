"""Shared test fixtures. Pytest loads this file automatically."""

import json
from pathlib import Path

import pytest

from revenue_agents.models import Deal, Lead, LeadScore, Stage, SubScore, Tier
from revenue_agents.segments import SegmentRegistry
from revenue_agents.skills.enrichment import JsonEnricher

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="session")
def leads():
    """All mock leads, by id."""
    return {item["id"]: Lead.model_validate(item) for item in json.loads((DATA / "leads.json").read_text())}


@pytest.fixture(scope="session")
def enricher():
    return JsonEnricher()


@pytest.fixture(scope="session")
def brand_pack():
    return SegmentRegistry().get("brand")


@pytest.fixture
def make_deal():
    """Build a deal at a given stage and tier (tier=None means not scored yet)."""

    def build(stage=Stage.SCORED, tier=Tier.A, segment="brand", deal_id="D-001"):
        score = None
        if tier is not None:
            sub = SubScore(value=50, reason="test")
            score = LeadScore(fit=sub, intent=sub, engagement=sub, total=50, tier=tier)
        return Deal(id=deal_id, lead_id="L-001", segment=segment, stage=stage, score=score)

    return build
