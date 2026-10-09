"""Enrichment skill: look up company facts for a lead.

Agents depend on the Enricher interface, not on a specific provider, so the
mock JSON provider can be swapped for a real one (Clearbit, Apollo,
ZoomInfo) without changing any agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from revenue_agents.models import Enrichment, Lead

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class EnrichmentNotFoundError(LookupError):
    """The provider has no data for this lead's company."""


class Enricher(Protocol):
    """Anything that can enrich a lead."""

    def enrich(self, lead: Lead) -> Enrichment: ...


class JsonEnricher:
    """Mock enrichment provider backed by a JSON file keyed by company domain."""

    def __init__(self, path: Path = DATA_DIR / "enrichment.json") -> None:
        records = json.loads(Path(path).read_text())
        self._records = {domain: Enrichment.model_validate(record) for domain, record in records.items()}

    def enrich(self, lead: Lead) -> Enrichment:
        record = self._records.get(lead.domain)
        if record is None:
            raise EnrichmentNotFoundError(f"no enrichment data for {lead.company} ({lead.domain})")
        return record.model_copy(deep=True)
