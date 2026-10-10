"""Composition root: builds the whole system and wires its parts together.

This is the one place that picks concrete classes (MockHubSpot, file-based
stores, the given LLM). Everything else depends on interfaces, so tests,
the demo script and the UI build the same system with different parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from revenue_agents.agents.call_intel import CallIntelAgent
from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.agents.outreach import OutreachAgent
from revenue_agents.agents.reply_triage import ReplyTriageAgent
from revenue_agents.agents.research import ResearchAgent
from revenue_agents.agents.scoring import ScoringAgent
from revenue_agents.crm import MockHubSpot
from revenue_agents.demo_llm import demo_llm
from revenue_agents.guardrails.approvals import ApprovalQueue
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.gateway import ActionGateway
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import AutonomyPolicy
from revenue_agents.handlers import ActionHandlers
from revenue_agents.lessons import LessonStore
from revenue_agents.llm import LLM, ClaudeLLM
from revenue_agents.orchestrator import Agents, Orchestrator
from revenue_agents.segments import SEGMENTS_DIR, SegmentRegistry
from revenue_agents.skills.enrichment import JsonEnricher
from revenue_agents.storage import STATE_DIR


@dataclass
class System:
    crm: MockHubSpot
    lessons: LessonStore
    approvals: ApprovalQueue
    audit: AuditLog
    kill_switch: KillSwitch
    segments: SegmentRegistry
    gateway: ActionGateway
    orchestrator: Orchestrator


def build_system(llm: LLM, state_dir: Path = STATE_DIR, segments_dir: Path = SEGMENTS_DIR) -> System:
    state_dir = Path(state_dir)
    segments = SegmentRegistry(segments_dir)
    crm = MockHubSpot(state_dir / "hubspot.json")
    lessons = LessonStore(state_dir / "lessons.json")
    approvals = ApprovalQueue(state_dir / "approvals.json")
    audit = AuditLog(state_dir / "audit.jsonl")
    kill_switch = KillSwitch(state_dir / "KILL_SWITCH")

    gateway = ActionGateway(
        AutonomyPolicy(segments, kill_switch), approvals, audit,
        ActionHandlers(crm, lessons).as_dict(), ActionClaims(state_dir / "claims"),
    )
    agents = Agents(
        research=ResearchAgent(JsonEnricher()),
        scoring=ScoringAgent(segments),
        outreach=OutreachAgent(llm, segments),
        reply_triage=ReplyTriageAgent(llm),
        call_intel=CallIntelAgent(llm, segments),
        deal_desk=DealDeskAgent(segments),
    )
    orchestrator = Orchestrator(crm, lessons, gateway, approvals, audit, segments, agents,
                                ActionClaims(state_dir / "events"))
    return System(crm, lessons, approvals, audit, kill_switch, segments, gateway, orchestrator)


def make_llm(mode: str) -> LLM:
    """`claude`: the real model (needs ANTHROPIC_API_KEY with credit). `fake`: scripted demo answers.

    There is no silent fallback: if `claude` is chosen and fails, the run fails.
    """
    if mode == "claude":
        return ClaudeLLM()
    if mode == "fake":
        return demo_llm()
    raise ValueError(f"unknown LLM mode '{mode}': use 'fake' or 'claude'")
