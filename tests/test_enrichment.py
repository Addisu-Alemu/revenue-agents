"""The mock enrichment provider finds company facts by domain."""

import json

import pytest

from revenue_agents.skills.enrichment import EnrichmentNotFoundError, JsonEnricher


def test_enriches_lead_by_domain(leads, enricher):
    enrichment = enricher.enrich(leads["L-001"])
    assert enrichment.locations == 420
    assert enrichment.business_model == "franchise"


def test_unknown_domain_raises_clear_error(leads, enricher):
    stranger = leads["L-001"].model_copy(update={"domain": "unknown.example"})
    with pytest.raises(EnrichmentNotFoundError, match="unknown.example"):
        enricher.enrich(stranger)


def test_returns_a_copy_so_callers_cannot_corrupt_the_source(leads, enricher):
    enricher.enrich(leads["L-001"]).signals.clear()
    assert enricher.enrich(leads["L-001"]).signals


def test_provider_data_source_is_swappable(tmp_path, leads):
    record = {"industry": "Test", "business_model": "franchise", "employees": 1, "locations": 7,
              "hq_country": "US", "annual_ad_spend_usd": 0}
    path = tmp_path / "other_provider.json"
    path.write_text(json.dumps({"brightbite.example": record}))

    assert JsonEnricher(path).enrich(leads["L-001"]).locations == 7
