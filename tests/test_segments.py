"""Segment packs load from YAML, and broken packs fail with a clear error."""

import pytest
import yaml

from revenue_agents.segments import SEGMENTS_DIR, SegmentConfigError, SegmentNotFoundError, SegmentRegistry


@pytest.fixture
def brand_config():
    """The real brand pack as a dict, so tests can break one thing at a time."""
    return yaml.safe_load((SEGMENTS_DIR / "brand.yaml").read_text())


def registry_with(tmp_path, config, name="brand"):
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(config))
    return SegmentRegistry(tmp_path)


def test_brand_pack_loads():
    pack = SegmentRegistry().get("brand")
    assert pack.segment == "brand"
    assert pack.scoring.weights.fit == 0.5
    assert pack.scoring.fit_rules[0].reason


def test_registry_lists_brand():
    assert "brand" in SegmentRegistry().names()


def test_unknown_segment_lists_available_ones():
    with pytest.raises(SegmentNotFoundError, match="Available: .*brand"):
        SegmentRegistry().get("publisher")


def test_segment_name_cannot_escape_folder():
    with pytest.raises(SegmentNotFoundError, match="invalid segment name"):
        SegmentRegistry().get("../secrets")


def test_weights_must_add_up_to_one(tmp_path, brand_config):
    brand_config["scoring"]["weights"]["fit"] = 0.9
    with pytest.raises(SegmentConfigError, match="add up to 1"):
        registry_with(tmp_path, brand_config).get("brand")


def test_fit_rule_with_unknown_field_is_rejected(tmp_path, brand_config):
    brand_config["scoring"]["fit_rules"][0]["field"] = "locatons"
    with pytest.raises(SegmentConfigError, match="unknown enrichment field 'locatons'"):
        registry_with(tmp_path, brand_config).get("brand")


def test_fit_rule_needs_exactly_one_check(tmp_path, brand_config):
    brand_config["scoring"]["fit_rules"][0]["gte"] = 5  # now has one_of AND gte
    with pytest.raises(SegmentConfigError, match="exactly one check"):
        registry_with(tmp_path, brand_config).get("brand")


def test_tier_a_must_be_above_tier_b(tmp_path, brand_config):
    brand_config["scoring"]["tiers"] = {"A": 30, "B": 70}
    with pytest.raises(SegmentConfigError, match="tier A threshold"):
        registry_with(tmp_path, brand_config).get("brand")


def test_unknown_engagement_field_is_rejected(tmp_path, brand_config):
    brand_config["scoring"]["engagement_points"]["tweets"] = 5
    with pytest.raises(SegmentConfigError, match="unknown engagement fields"):
        registry_with(tmp_path, brand_config).get("brand")


def test_file_name_must_match_segment(tmp_path, brand_config):
    with pytest.raises(SegmentConfigError, match="expected 'retail'"):
        registry_with(tmp_path, brand_config, name="retail").get("retail")
