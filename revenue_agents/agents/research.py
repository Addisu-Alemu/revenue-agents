"""Research agent: find out who a new lead is and why they might buy now, then score them.

The model decides what to look at and writes the "why now" note that the
outreach agent builds its opening on. Facts come from the enrichment provider,
and the score from the segment's rules in plain code (the score_lead tool):
the model can ask for a score but can't change a number, because the tier
decides whether a human approves first contact.
"""

from __future__ import annotations

from pydantic import Field

from revenue_agents.agents.base import Agent
from revenue_agents.crm import CRM
from revenue_agents.guardrails.gateway import ActionResult
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.guardrails.untrusted import UNTRUSTED_RULES, wrap_untrusted
from revenue_agents.models import Deal, Enrichment, Lead, Model
from revenue_agents.runtime import AgentTask, NoInput, Script, Tool, ToolResult
from revenue_agents.segments import SegmentRegistry
from revenue_agents.skills.enrichment import Enricher, EnrichmentNotFoundError
from revenue_agents.skills.scoring import score_lead

SYSTEM_PROMPT = f"""You research new leads for Cool AI, an AI advertising platform (creative automation, media
activation, measurement).

{UNTRUSTED_RULES}

Use your tools in this order: read the lead, enrich the company, score the lead, then save a research note.
The score comes from fixed rules: report it, never argue with it.
The note has two parts:
- why_now: one or two sentences on why this company might buy now. Cite one specific signal or fact the tools
  returned (an expansion, a campaign, a hire). If there is no signal, say so plainly.
- angle: which pain or value proposition to lead with, in a few words.
Use only facts the tools returned. Never invent numbers, events or names.
When the note is saved, reply with one short sentence and stop."""


class ResearchNote(Model):
    why_now: str = Field(max_length=400)
    angle: str = Field(max_length=200)


class ResearchAgent(Agent):
    name = "research"

    def __init__(self, crm: CRM, enricher: Enricher, segments: SegmentRegistry) -> None:
        self._crm = crm
        self._enricher = enricher
        self._segments = segments

    def task(self, lead: Lead, deal: Deal) -> AgentTask:
        found: dict[str, Enrichment] = {}

        def lookup() -> Enrichment:
            if "facts" not in found:  # the hook and the tool body both need it: ask the provider once
                found["facts"] = self._enricher.enrich(lead)
            return found["facts"]

        def read_lead(_: NoInput) -> ToolResult:
            e = lead.engagement
            return ToolResult(text=(
                f"{lead.company} ({lead.domain}), segment {lead.segment}, source {lead.source}.\n"
                f"Contact: {lead.contact.name}, {lead.contact.title}.\n"
                f"Engagement so far: {e.email_opens} email opens, {e.link_clicks} clicks, {e.website_visits} site "
                f"visits, {e.pricing_page_visits} pricing page visits."))

        def no_enrichment(_: NoInput) -> str | None:
            try:
                lookup()
            except EnrichmentNotFoundError as error:
                return str(error)
            return None

        def enrich(_: NoInput):
            facts = lookup()
            signals = ", ".join(signal.type for signal in facts.signals) or "none"
            return self.propose(ActionType.ENRICH_LEAD, deal, key="enrich",
                                summary=f"Enrich {lead.company}: {facts.locations} locations, signals: {signals}",
                                payload={"enrichment": facts.model_dump(mode="json")})

        def enriched(_: NoInput, result: ActionResult) -> ToolResult:
            return ToolResult(text=_describe(lookup(), lead), data={"enrichment": lookup().model_dump(mode="json")})

        def not_enriched(_: NoInput) -> str | None:
            return None if self._crm.get_deal(deal.id).enrichment else "Enrich the company first (enrich_company)"

        def score(_: NoInput):
            facts = self._crm.get_deal(deal.id).enrichment
            result = score_lead(lead, facts, self._segments.get(lead.segment).scoring)
            return self.propose(ActionType.SCORE_LEAD, deal, key="score",
                                summary=f"Score {lead.company}: {result.total:g} (Tier {result.tier.value})",
                                payload={"score": result.model_dump(mode="json")})

        def scored(_: NoInput, result: ActionResult) -> ToolResult:
            s = self._crm.get_deal(deal.id).score
            return ToolResult(
                text=(f"Total {s.total:g}, Tier {s.tier.value}.\nFit {s.fit.value:g}: {s.fit.reason}\n"
                      f"Intent {s.intent.value:g}: {s.intent.reason}\nEngagement {s.engagement.value:g}: "
                      f"{s.engagement.reason}"),
                data={"score": s.model_dump(mode="json")})

        def note(args: ResearchNote):
            return self.propose(ActionType.RECORD_RESEARCH, deal, key="note",
                                summary=f"Why now for {lead.company}: {args.why_now}",
                                payload={"why_now": args.why_now.strip(), "angle": args.angle.strip()})

        def script(s: Script) -> None:
            s.call("read_lead")
            if s.call("enrich_company").is_error:
                s.say(f"No company data for {lead.company}: a human should research it.")
                return
            scored_result = s.call("score_lead")
            s.call("save_research_note", **s.ask(ResearchNote).model_dump())
            s.say(f"{lead.company} researched: {scored_result.text.splitlines()[0]}")

        tools = [
            Tool("read_lead", "Read the lead as it arrived in the CRM: company, contact, source and engagement.",
                 NoInput, run=read_lead),
            Tool("enrich_company", "Look up company facts and buying signals with the enrichment provider, and save "
                                   "them on the deal.", NoInput, action=enrich, done=enriched, check=no_enrichment,
                 proposes=ActionType.ENRICH_LEAD),
            Tool("score_lead", "Score the lead with the segment's fixed rules (fit, intent, engagement) and save the "
                               "score and tier on the deal. Needs the enrichment first.",
                 NoInput, action=score, done=scored, check=not_enriched, proposes=ActionType.SCORE_LEAD),
            Tool("save_research_note", "Save why this company might buy now and what to lead with, for the outreach "
                                       "agent.", ResearchNote, action=note,
                 proposes=ActionType.RECORD_RESEARCH),
        ]
        return AgentTask(agent=self.name, title=f"Research {lead.company}", deal_id=deal.id, system=SYSTEM_PROMPT,
                         prompt=(f"New lead: {lead.company} ({lead.domain}), {lead.contact.name}, "
                                 f"{lead.contact.title}. Segment: {lead.segment}.\nResearch and score this lead."),
                         tools=tools, script=script, max_turns=10)


def _describe(facts: Enrichment, lead: Lead) -> str:
    """Company facts for the model. They come from an outside provider, so they are data, not instructions."""
    lines = [f"Industry: {facts.industry}; model: {facts.business_model}; {facts.locations} locations; "
             f"{facts.employees} employees; HQ {facts.hq_country}; ${facts.annual_ad_spend_usd:,} a year on ads",
             f"Tech stack: {', '.join(facts.tech_stack) or 'unknown'}"]
    if facts.managed_clients is not None:
        lines.append(f"Managed clients: {facts.managed_clients}")
    lines += [f"Signal ({signal.type}, {signal.date}): {signal.detail}" for signal in facts.signals] or ["No signals."]
    return wrap_untrusted("\n".join(lines), f"enrichment:{lead.domain}")
