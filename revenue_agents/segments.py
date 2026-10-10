"""Segment config packs.

Everything that differs between customer segments (the ideal customer, how
to score them, what to say to them) lives in segments/<name>.yaml. Agents
and skills only read a SegmentPack, so adding a segment means adding one
YAML file and no code.
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from revenue_agents.models import Engagement, Enrichment, Model, Tier

SEGMENTS_DIR = Path(__file__).resolve().parent.parent / "segments"
SEGMENT_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


class SegmentNotFoundError(LookupError):
    """No config pack exists for the requested segment."""


class SegmentConfigError(ValueError):
    """A config pack exists but is invalid."""


# --- Pack schema -----------------------------------------------------------


class FitRule(Model):
    """Award points when one enrichment field passes one check."""

    field: str
    points: float = Field(gt=0, le=100)
    reason: str
    gte: float | None = None
    one_of: list[str] | None = None
    contains_any: list[str] | None = None

    @field_validator("field")
    @classmethod
    def field_must_exist(cls, value: str) -> str:
        if value not in Enrichment.model_fields:
            raise ValueError(f"unknown enrichment field '{value}'. Valid fields: {sorted(Enrichment.model_fields)}")
        return value

    @model_validator(mode="after")
    def exactly_one_check(self) -> FitRule:
        checks = [check for check in (self.gte, self.one_of, self.contains_any) if check is not None]
        if len(checks) != 1:
            raise ValueError("a fit rule needs exactly one check: gte, one_of or contains_any")
        return self


class Weights(Model):
    fit: float = Field(ge=0, le=1)
    intent: float = Field(ge=0, le=1)
    engagement: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def must_add_up_to_one(self) -> Weights:
        total = self.fit + self.intent + self.engagement
        if abs(total - 1) > 1e-6:
            raise ValueError(f"weights must add up to 1, got {total:g}")
        return self


class TierThresholds(Model):
    A: float = Field(ge=0, le=100)
    B: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def a_above_b(self) -> TierThresholds:
        if self.A <= self.B:
            raise ValueError("tier A threshold must be higher than tier B")
        return self


class ScoringConfig(Model):
    weights: Weights
    tiers: TierThresholds
    fit_rules: list[FitRule] = Field(min_length=1)
    intent_points: dict[str, float]
    engagement_points: dict[str, float]

    @field_validator("engagement_points")
    @classmethod
    def engagement_keys_must_exist(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = set(value) - set(Engagement.model_fields)
        if unknown:
            raise ValueError(f"unknown engagement fields {sorted(unknown)}. Valid fields: {sorted(Engagement.model_fields)}")
        return value


class Messaging(Model):
    tone: str
    value_props: list[str] = Field(min_length=1)
    call_to_action: str
    avoid_phrases: list[str] = Field(default_factory=list)
    # Tiers that get a first-touch email. Others are left for nurture.
    outreach_tiers: list[Tier] = Field(default_factory=lambda: [Tier.A, Tier.B])


SUBJECT_PLACEHOLDERS = frozenset({"company", "first_name"})
BODY_PLACEHOLDERS = frozenset({"company", "first_name", "opening", "value_prop", "call_to_action", "sender_name"})


def _placeholders(template: str) -> set[str]:
    try:
        return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as error:  # e.g. an unclosed "{"
        raise ValueError(f"template is malformed: {error}") from error


class Templates(Model):
    first_touch_subject: str
    first_touch_body: str

    @field_validator("first_touch_subject")
    @classmethod
    def subject_placeholders_known(cls, value: str) -> str:
        return _check_placeholders(value, SUBJECT_PLACEHOLDERS)

    @field_validator("first_touch_body")
    @classmethod
    def body_placeholders_known(cls, value: str) -> str:
        return _check_placeholders(value, BODY_PLACEHOLDERS)


def _check_placeholders(template: str, allowed: frozenset[str]) -> str:
    unknown = _placeholders(template) - allowed
    if unknown:
        raise ValueError(f"unknown placeholders {sorted(unknown)}. Allowed: {sorted(allowed)}")
    return template


class AutonomyConfig(Model):
    """Segment-level autonomy settings. A pack may tighten the policy, never loosen it."""

    first_touch_approval_tiers: list[Tier] = Field(default_factory=lambda: [Tier.A])

    @field_validator("first_touch_approval_tiers")
    @classmethod
    def tier_a_always_needs_approval(cls, value: list[Tier]) -> list[Tier]:
        if Tier.A not in value:
            raise ValueError("first contact with Tier A leads always needs approval; a pack can add tiers but not remove A")
        return value


class SegmentPack(Model):
    """One segment's complete configuration."""

    segment: str
    display_name: str
    icp_summary: str
    products: list[str] = Field(min_length=1)
    scoring: ScoringConfig
    messaging: Messaging
    templates: Templates
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)


# --- Loading ---------------------------------------------------------------


class SegmentRegistry:
    """Finds, loads and validates segment packs from a folder of YAML files."""

    def __init__(self, directory: Path = SEGMENTS_DIR) -> None:
        self._directory = Path(directory)
        self._cache: dict[str, SegmentPack] = {}

    def names(self) -> list[str]:
        return sorted(path.stem for path in self._directory.glob("*.yaml"))

    def get(self, name: str) -> SegmentPack:
        if name not in self._cache:
            self._cache[name] = self._load(name)
        return self._cache[name]

    def _load(self, name: str) -> SegmentPack:
        # Segment names come from lead data, so never let one escape the folder.
        if not SEGMENT_NAME.match(name):
            raise SegmentNotFoundError(f"invalid segment name '{name}'")

        path = self._directory / f"{name}.yaml"
        if not path.is_file():
            available = ", ".join(self.names()) or "none"
            raise SegmentNotFoundError(f"no config pack for segment '{name}'. Available: {available}")

        try:
            pack = SegmentPack.model_validate(yaml.safe_load(path.read_text()) or {})
        except (yaml.YAMLError, ValidationError) as error:
            raise SegmentConfigError(f"{path.name} is invalid:\n{error}") from error

        if pack.segment != name:
            raise SegmentConfigError(f"{path.name} declares segment '{pack.segment}', expected '{name}'")
        return pack
