"""Composition root: builds the whole system and wires its parts together.

This is the one place that picks concrete classes (MockHubSpot, file-based
stores, the given LLM). Everything else depends on interfaces, so tests,
the demo script and the UI build the same system with different parts.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from revenue_agents.agents.call_brief import CallBriefAgent
from revenue_agents.agents.call_intel import CallIntelAgent
from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.agents.outreach import OutreachAgent
from revenue_agents.agents.reply_triage import ReplyTriageAgent
from revenue_agents.agents.research import ResearchAgent
from revenue_agents.agents.scoring import ScoringAgent
from revenue_agents.crm import MockHubSpot
from revenue_agents.demo_llm import demo_llm
from revenue_agents.esign import MockESign
from revenue_agents.guardrails.approvals import ApprovalQueue
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.gateway import ActionGateway
from revenue_agents.guardrails.held_events import HeldEvents
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import AutonomyPolicy
from revenue_agents.handlers import ActionHandlers
from revenue_agents.lessons import LessonStore
from revenue_agents.llm import LLM, ClaudeLLM
from revenue_agents.orchestrator import Agents, Orchestrator
from revenue_agents.segments import SEGMENTS_DIR, SegmentRegistry
from revenue_agents.skills.enrichment import JsonEnricher
from revenue_agents.storage import STATE_DIR
from revenue_agents.tracing import TracedAgent, Tracer, TracingLLM
from revenue_agents.tracking import Tracker


@dataclass
class System:
    crm: MockHubSpot
    esign: MockESign
    lessons: LessonStore
    approvals: ApprovalQueue
    audit: AuditLog
    kill_switch: KillSwitch
    segments: SegmentRegistry
    gateway: ActionGateway
    orchestrator: Orchestrator
    tracer: Tracer


def build_system(llm: LLM, state_dir: Path = STATE_DIR, segments_dir: Path = SEGMENTS_DIR) -> System:
    state_dir = Path(state_dir)
    segments = SegmentRegistry(segments_dir)
    crm = MockHubSpot(state_dir / "hubspot.json")
    lessons = LessonStore(state_dir / "lessons.json")
    approvals = ApprovalQueue(state_dir / "approvals.json")
    audit = AuditLog(state_dir / "audit.jsonl")
    kill_switch = KillSwitch(state_dir / "KILL_SWITCH")
    esign = MockESign(state_dir / "esign.json")

    tracer = Tracer(state_dir / "traces.jsonl")
    llm = TracingLLM(llm, tracer)
    claims = ActionClaims(state_dir / "claims")
    gateway = ActionGateway(
        AutonomyPolicy(segments, kill_switch), approvals, audit, ActionHandlers(crm, lessons, esign).as_dict(), claims,
        tracer,
    )

    def traced(agent):
        return TracedAgent(agent, tracer)

    agents = Agents(
        research=traced(ResearchAgent(JsonEnricher())),
        scoring=traced(ScoringAgent(segments)),
        outreach=traced(OutreachAgent(llm, segments)),
        reply_triage=traced(ReplyTriageAgent(llm)),
        call_brief=traced(CallBriefAgent(segments)),
        call_intel=traced(CallIntelAgent(llm, segments)),
        deal_desk=traced(DealDeskAgent(segments)),
    )
    held = HeldEvents(state_dir / "held_events.json")
    tracker = Tracker(crm, approvals, lessons, segments, claims, state_dir / "digests", held)
    orchestrator = Orchestrator(crm, lessons, gateway, approvals, audit, segments, agents,
                                ActionClaims(state_dir / "events"), tracker, esign, kill_switch, held, tracer)
    return System(crm, esign, lessons, approvals, audit, kill_switch, segments, gateway, orchestrator, tracer)


def make_llm(mode: str) -> LLM:
    """`claude`: the real model (needs ANTHROPIC_API_KEY with credit). `scripted`: pre-written demo answers.

    There is no silent fallback: if `claude` is chosen and fails, the run fails.
    """
    if mode == "claude":
        return ClaudeLLM()
    if mode == "scripted":
        return demo_llm()
    raise ValueError(f"unknown LLM mode '{mode}': use 'scripted' or 'claude'")


def reset_state(state_dir: Path) -> None:
    """Delete all runtime state except the kill switch, so a reset can never silently turn it off."""
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return
    for path in state_dir.iterdir():
        if path.name == "KILL_SWITCH":
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
