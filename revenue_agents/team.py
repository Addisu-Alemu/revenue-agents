"""The agent team and its rules, as data built from the code, so the Team page can't drift from what runs.

Each AI agent's tools are read from a sample task (nothing is written), and
each tool's permission comes from the autonomy policy and the segment packs:
what it proposes, and whether that runs on its own, asks a human, or can't run.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from revenue_agents.guardrails.policy import AGENT_PERMISSIONS, ALWAYS_NEEDS_APPROVAL, ActionType
from revenue_agents.models import Deal, Message, Reply, Stage
from revenue_agents.runtime import Tool
from revenue_agents.system import System

JOBS = {
    "research": "Finds out who a new lead is and why they might buy now, then scores them with the segment's rules.",
    "outreach": "Writes first emails in the segment's voice, using what the team has learned, and fixes its own "
                "draft until code accepts it.",
    "reply": "Reads every reply as untrusted text, decides the next step: answers questions, books meetings, sets "
             "follow-ups, hands the rest to a human.",
    "call_intel": "Turns call transcripts into deal fields (checked by code) and lessons for the next deals.",
}
SKILLS = {
    "Scoring": "Fit, intent and engagement from the segment pack, in plain code: the tier decides who needs a human, "
               "so it must be exact and explainable. The research agent calls it as a tool.",
    "Call brief": "A one-page prep sheet for the rep before each meeting: what we know, lessons, what to ask.",
    "Deal desk": "Builds the IO from the deal's checked fields and the template. Contracts must be exact, so no model "
                 "writes them; sending always waits for a human.",
}
LABELS = {"research": "Research agent", "outreach": "Outreach agent", "reply": "Reply agent",
          "call_intel": "Call intel agent", "call_brief": "Call brief (code skill)", "deal_desk": "Deal desk (code skill)",
          "orchestrator": "Orchestrator"}


@dataclass(frozen=True)
class ToolCard:
    name: str
    description: str
    proposes: ActionType | None
    permission: str  # "reads only", "on its own", "asks you ..." or "always asks you"
    guarded: bool  # has a rule in code that can refuse it


@dataclass(frozen=True)
class AgentCard:
    name: str
    label: str
    job: str
    tools: list[ToolCard]


@dataclass(frozen=True)
class Rule:
    applies_to: str
    rule: str
    decision: str
    who_changes_it: str


def roster(system: System) -> list[AgentCard]:
    agents = system.orchestrator._agents
    lead = system.crm.leads()[0]
    deal = Deal(id="D-preview", lead_id=lead.id, segment=lead.segment, stage=Stage.OUTREACH)
    now = dt.datetime.now(dt.timezone.utc)
    reply = Reply.model_validate({"id": "R-preview", "lead_id": lead.id, "from": lead.contact.email, "subject": "Re: hi",
                                  "received_at": now, "body": "Thanks."})
    ours = Message(id="msg:preview", thread_id="T-preview", deal_id=deal.id, direction="outbound", sender="us",
                   to=lead.contact.email, subject="hi", body="Hi.", at=now)
    tasks = [agents.research.task(lead, deal), agents.outreach.first_touch(lead, deal),
             agents.reply.task(reply, lead, deal, ours), agents.call_intel.task("preview", "", lead, deal)]
    email = email_rule(system)
    return [AgentCard(task.agent, LABELS[task.agent], JOBS[task.agent],
                      [_tool(tool, email) for tool in task.tools]) for task in tasks]


def email_rule(system: System) -> str:
    tiers = {name: "/".join(t.value for t in system.segments.get(name).autonomy.first_touch_approval_tiers)
             for name in system.segments.names()}
    by_pack = ", ".join(f"{name}: Tier {value}" for name, value in tiers.items())
    return f"asks you for top tiers ({by_pack}) and anything about price"


def rules(system: System) -> list[Rule]:
    """The autonomy policy (guardrails/policy.py) and the tools' own rules, in the order they apply."""
    tiers = {name: "/".join(t.value for t in system.segments.get(name).autonomy.first_touch_approval_tiers)
             for name in system.segments.names()}
    by_pack = "; ".join(f"{name}: Tier {value}" for name, value in tiers.items())
    high_risk = ", ".join(sorted(action.value for action in ALWAYS_NEEDS_APPROVAL))
    return [
        Rule("Any side effect while agents are paused", "kill_switch", "nothing runs; events wait in order", "you"),
        Rule("A tool that isn't on the agent's list", "scoped_tools / agent_scope", "refused", "code only"),
        Rule(f"{high_risk}", "high_risk_action", "always asks you", "code only"),
        Rule("An email with prices, rates, a rate card or discounts", "pricing_in_email", "asks you", "code only"),
        Rule("An email to a lead that has no score yet", "unscored_lead", "asks you", "code only"),
        Rule("First email to a top-tier lead", "first_touch_tier", f"asks you ({by_pack})", "segment pack"),
        Rule("An answer to a top-tier lead's reply", "reply_to_top_tier", f"asks you ({by_pack})", "segment pack"),
        Rule("Drafting an answer to a pricing question or a suspicious message", "tool_rule", "refused: a human "
             "answers", "code only"),
        Rule("Booking a meeting nobody asked for; a date in the past", "tool_rule", "refused", "code only"),
        Rule("Learning from a message with instructions aimed at an AI", "tool_rule", "refused", "code only"),
        Rule("Voiding an outdated IO", "low_risk (protective)", "on its own", "code only"),
        Rule("CRM updates, research notes, lessons, meetings, follow-ups, other emails", "low_risk", "on its own",
             "code only"),
    ]


def permissions(agent: str) -> list[str]:
    return sorted(action.value for action in AGENT_PERMISSIONS.get(agent, frozenset()))


def _tool(tool: Tool, email: str) -> ToolCard:
    if not tool.effect:
        permission = "no side effects"
    elif tool.proposes in ALWAYS_NEEDS_APPROVAL:
        permission = "always asks you"
    elif tool.proposes is ActionType.SEND_EMAIL:
        permission = email
    else:
        permission = "on its own"
    return ToolCard(tool.name, tool.description, tool.proposes, permission, tool.check is not None)
