"""Composition root: builds the whole system and wires its parts together.

This is the one place that picks concrete classes (MockHubSpot, file-based
stores, the given agent runtime). Everything else depends on interfaces, so
tests, the demo script and the UI build the same system with different parts.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from revenue_agents.agents.call_brief import CallBriefAgent
from revenue_agents.agents.call_intel import CallIntelAgent
from revenue_agents.agents.copilot import CopilotAgent
from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.agents.outreach import OutreachAgent
from revenue_agents.agents.reply import ReplyAgent
from revenue_agents.agents.research import ResearchAgent
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
from revenue_agents.orchestrator import Agents, Orchestrator
from revenue_agents.runtime import AgentHost, ClaudeRuntime, Runtime, ScriptedRuntime, ToolGuard
from revenue_agents.segments import SEGMENTS_DIR, SegmentRegistry
from revenue_agents.skills.enrichment import JsonEnricher
from revenue_agents.storage import STATE_DIR
from revenue_agents.tracing import TracedAgent, Tracer
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
    runtime: Runtime


def build_system(runtime: Runtime, state_dir: Path = STATE_DIR, segments_dir: Path = SEGMENTS_DIR) -> System:
    state_dir = Path(state_dir)
    segments = SegmentRegistry(segments_dir)
    crm = MockHubSpot(state_dir / "hubspot.json")
    lessons = LessonStore(state_dir / "lessons.json")
    approvals = ApprovalQueue(state_dir / "approvals.json")
    audit = AuditLog(state_dir / "audit.jsonl")
    kill_switch = KillSwitch(state_dir / "KILL_SWITCH")
    esign = MockESign(state_dir / "esign.json")

    tracer = Tracer(state_dir / "traces.jsonl")
    claims = ActionClaims(state_dir / "claims")
    gateway = ActionGateway(
        AutonomyPolicy(segments, kill_switch), approvals, audit, ActionHandlers(crm, lessons, esign).as_dict(), claims,
        tracer,
    )

    agents = Agents(
        host=AgentHost(runtime, ToolGuard(gateway, crm), tracer),
        research=ResearchAgent(crm, JsonEnricher(), segments),
        outreach=OutreachAgent(crm, lessons, segments),
        reply=ReplyAgent(crm, lessons, segments),
        call_intel=CallIntelAgent(crm, segments),
        call_brief=TracedAgent(CallBriefAgent(segments), tracer),  # code skills: traced as one step each
        deal_desk=TracedAgent(DealDeskAgent(segments), tracer),
        copilot=CopilotAgent(crm, lessons, approvals),
    )
    held = HeldEvents(state_dir / "held_events.json")
    failed = HeldEvents(state_dir / "failed_events.json")
    tracker = Tracker(crm, approvals, lessons, segments, claims, state_dir / "digests", held, failed)
    orchestrator = Orchestrator(crm, lessons, gateway, approvals, audit, segments, agents,
                                ActionClaims(state_dir / "events"), tracker, esign, kill_switch, held, failed, tracer)
    return System(crm, esign, lessons, approvals, audit, kill_switch, segments, gateway, orchestrator, tracer, runtime)


def make_runtime(mode: str) -> Runtime:
    """`claude`: Claude picks the tools (needs ANTHROPIC_API_KEY with credit). `scripted`: pre-written decisions.

    There is no silent fallback: if `claude` is chosen and fails, the run fails.
    """
    if mode == "claude":
        return ClaudeRuntime()
    if mode == "scripted":
        return ScriptedRuntime(demo_llm())
    raise ValueError(f"unknown model mode '{mode}': use 'scripted' or 'claude'")


# Kept by a reset: the kill switch (a reset must never silently turn it off), and the lock a running world
# runner holds (see world.py).
KEPT_ON_RESET = frozenset({"KILL_SWITCH", "world.runner.lock"})


def reset_state(state_dir: Path) -> None:
    """Delete all runtime state except the kill switch, so a reset can never silently turn it off."""
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return
    for path in state_dir.iterdir():
        if path.name in KEPT_ON_RESET:
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
