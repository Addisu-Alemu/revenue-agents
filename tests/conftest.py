"""Shared test fixtures. Pytest loads this file automatically."""

import json
from pathlib import Path

import pytest

from revenue_agents.models import Lead
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
