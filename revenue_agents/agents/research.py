"""Research agent: enrich a new lead with company facts. No LLM."""

from __future__ import annotations

from revenue_agents.agents.base import Agent, AgentResult
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Deal, Enrichment, Lead
from revenue_agents.skills.enrichment import Enricher


class ResearchAgent(Agent):
    name = "research"

    def __init__(self, enricher: Enricher) -> None:
        self._enricher = enricher

    def run(self, lead: Lead, deal: Deal) -> AgentResult[Enrichment]:
        enrichment = self._enricher.enrich(lead)
        signals = ", ".join(signal.type for signal in enrichment.signals) or "none"
        action = self.propose(
            ActionType.ENRICH_LEAD,
            deal,
            key="enrich",
            summary=f"Enrich {lead.company}: {enrichment.locations} locations, signals: {signals}",
            payload={"enrichment": enrichment.model_dump(mode="json")},
        )
        return AgentResult(output=enrichment, actions=[action])
