"""Scoring agent: score and tier a lead with its segment's rules. No LLM, on purpose.

The tier decides whether a human approves first contact, so it must be
deterministic and explainable (see skills/scoring.py).
"""

from __future__ import annotations

from revenue_agents.agents.base import Agent, AgentResult
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Deal, Enrichment, Lead, LeadScore
from revenue_agents.segments import SegmentRegistry
from revenue_agents.skills.scoring import score_lead


class ScoringAgent(Agent):
    name = "scoring"

    def __init__(self, segments: SegmentRegistry) -> None:
        self._segments = segments

    def run(self, lead: Lead, deal: Deal, enrichment: Enrichment) -> AgentResult[LeadScore]:
        score = score_lead(lead, enrichment, self._segments.get(lead.segment).scoring)
        action = self.propose(
            ActionType.SCORE_LEAD,
            deal,
            key="score",
            summary=f"Score {lead.company}: {score.total:g} (Tier {score.tier.value})",
            payload={"score": score.model_dump(mode="json")},
        )
        return AgentResult(output=score, actions=[action])
