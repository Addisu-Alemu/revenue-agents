"""Call brief agent: a one-page prep sheet for the rep, created when a deal reaches Meeting.

No LLM: it assembles what the system already knows (company facts, why the
lead scored as it did, what the customer said, lessons from similar deals)
and turns the proposal fields that are still missing into questions to ask.
So the call collects exactly what the IO needs.
"""

from __future__ import annotations

from pydantic import Field

from revenue_agents.agents.base import Agent, AgentResult
from revenue_agents.crm import Activity
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Deal, Lead, Lesson, Model
from revenue_agents.segments import SegmentRegistry
from revenue_agents.stages import PROPOSAL_FIELDS

QUESTIONS = {
    "budget_usd": "What budget is set aside for this?",
    "products": "Which products, and how many locations, for a first phase?",
    "flight_start": "When should the campaign launch?",
    "flight_end": "How long should the first phase run?",
    "decision_maker": "Who signs the contract, and who else needs to approve it?",
}


class CallBrief(Model):
    deal_id: str
    company: str
    headline: str
    attendees: list[str]
    why_fit: list[str]
    what_they_said: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    products: list[str]
    questions: list[str]
    watch_outs: list[str] = Field(default_factory=list)

    def render(self) -> str:
        """The brief as a short markdown page."""
        sections = [
            ("Who's on the call", self.attendees),
            ("Why they're a fit", self.why_fit),
            ("What they've told us", self.what_they_said or ["Nothing in writing yet."]),
            ("Objections to expect", self.objections or ["None raised so far."]),
            ("Lessons from similar deals", self.lessons or ["None yet for this segment."]),
            ("What to propose", self.products),
            ("Questions to ask (needed before we can send a proposal)", self.questions or ["Nothing missing."]),
        ]
        if self.watch_outs:
            sections.append(("Watch out", self.watch_outs))
        body = "\n\n".join(f"**{title}**\n" + "\n".join(f"- {item}" for item in items) for title, items in sections)
        return f"### Call brief: {self.company}\n{self.headline}\n\n{body}"


class CallBriefAgent(Agent):
    name = "call_brief"

    def __init__(self, segments: SegmentRegistry) -> None:
        self._segments = segments

    def run(self, lead: Lead, deal: Deal, timeline: list[Activity], lessons: list[Lesson]) -> AgentResult[CallBrief]:
        pack = self._segments.get(lead.segment)
        relevant = [lesson for lesson in lessons if lesson.segment == lead.segment and "call_brief" in lesson.applies_to]
        replies = [activity.data.get("classification", {}) for activity in timeline if activity.kind == "reply"]

        brief = CallBrief(
            deal_id=deal.id,
            company=lead.company,
            headline=_headline(deal),
            attendees=[f"{lead.contact.name}, {lead.contact.title} ({lead.contact.email})"],
            why_fit=_why_fit(deal),
            what_they_said=[line for reply in replies for line in _said(reply)],
            objections=sorted({objection for reply in replies for objection in reply.get("objections", [])}),
            lessons=[f"{lesson.text} (from {lesson.source} {lesson.source_id})" for lesson in relevant],
            products=pack.products,
            questions=[QUESTIONS[name] for name in PROPOSAL_FIELDS if not getattr(deal.fields, name)],
            watch_outs=_watch_outs(deal.flags),
        )
        action = self.propose(
            ActionType.PREPARE_CALL_BRIEF, deal, key="meeting",
            summary=f"Call brief ready for the rep: {lead.company}, meeting with {lead.contact.name}",
            payload={"brief": brief.model_dump(mode="json"), "markdown": brief.render(),
                     "lessons_used": [lesson.id for lesson in relevant]},
        )
        return AgentResult(output=brief, actions=[action])


def _headline(deal: Deal) -> str:
    if not deal.score:
        return "Not scored yet."
    s = deal.score
    return (f"Tier {s.tier.value} · {s.total:g} (fit {s.fit.value:g}, intent {s.intent.value:g}, "
            f"engagement {s.engagement.value:g})")


def _why_fit(deal: Deal) -> list[str]:
    e = deal.enrichment
    if e is None:
        return ["No company facts yet."]
    facts = [f"{e.industry} ({e.business_model}), {e.employees:,} employees, {e.locations} locations, "
             f"${e.annual_ad_spend_usd:,} a year on ads" + (f", uses {', '.join(e.tech_stack)}" if e.tech_stack else "")]
    return facts + [f"Signal: {signal.detail} ({signal.date})" for signal in e.signals]


def _said(reply: dict) -> list[str]:
    lines = [reply["summary"]] if reply.get("summary") else []
    if reply.get("suggested_next_step"):
        lines.append(f"Suggested next step: {reply['suggested_next_step']}")
    return lines


def _watch_outs(flags: list[str]) -> list[str]:
    notes = []
    if "pricing_requested" in flags:
        notes.append("They asked about pricing: agree it with your manager first (pricing always needs approval).")
    if "possible_prompt_injection" in flags:
        notes.append("A message from this account contained instructions aimed at our AI: ignore them, verify by phone.")
    if "reply_without_outreach" in flags:
        notes.append("They wrote before we emailed them: confirm who they are and how they found us.")
    return notes
