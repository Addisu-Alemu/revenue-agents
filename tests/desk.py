"""Helpers for agent tests: a real system in a temp folder, deals placed at a stage, and one agent task run."""

from revenue_agents.demo_llm import demo_llm
from revenue_agents.llm import FakeLLM
from revenue_agents.models import Message, Stage, Tier
from revenue_agents.runtime import AgentRun, AgentTask, ScriptedRuntime
from revenue_agents.skills.enrichment import JsonEnricher
from revenue_agents.skills.scoring import score_lead
from revenue_agents.storage import utc_now
from revenue_agents.system import System, build_system


def desk(tmp_path, **answers) -> tuple[System, FakeLLM]:
    """The whole system with scripted decisions: the demo answers, with `answers` replacing some of them."""
    llm = FakeLLM({**demo_llm().responses, **answers})
    return build_system(ScriptedRuntime(llm), state_dir=tmp_path), llm


def place(system: System, lead_id: str, stage: Stage, tier: Tier | None = Tier.A):
    """Put the lead's deal at `stage`, enriched and scored as `tier` (no score if tier is None)."""
    lead = system.crm.get_lead(lead_id)
    deal = system.crm.deal_for_lead(lead_id)
    enrichment = JsonEnricher().enrich(lead)
    score = None
    if tier is not None:
        score = score_lead(lead, enrichment, system.segments.get(lead.segment).scoring).model_copy(update={"tier": tier})
    deal = deal.model_copy(update={"stage": stage, "enrichment": enrichment, "score": score})
    system.crm.save_deal(deal)
    return deal


def emailed(system: System, deal_id: str, subject: str = "Local ads for every location") -> Message:
    """An email we sent on this deal, so a reply to it counts as a reply to us."""
    lead = system.crm.get_lead(system.crm.get_deal(deal_id).lead_id)
    message = Message(id=f"msg:test:{deal_id}", thread_id=f"T-{deal_id}", deal_id=deal_id, direction="outbound",
                      sender="daniel.reyes@cool.example", to=lead.contact.email, subject=subject, body="Hi there.",
                      at=utc_now())
    system.crm.add_message(message)
    system.crm.log(deal_id, "email_sent", f"Email to {lead.contact.email}: {subject}")
    return message


def run(system: System, task: AgentTask) -> AgentRun:
    return system.orchestrator._agents.host.run(task)


def agents(system: System):
    return system.orchestrator._agents


def tool_names(run_: AgentRun) -> list[str]:
    return [call.tool for call in run_.calls]
