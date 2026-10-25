"""Call intel agent: turn a call transcript (e.g. from Fireflies) into deal fields and lessons.

The model reads the transcript and the deal, then writes what it found with
its tools. Plain code checks every field before it can update the deal:
products must exist in the segment pack, flight dates must make sense, and
gaps that block a proposal are flagged and handed back to the model. Lessons
are never learned from a transcript that contains instructions aimed at an AI.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from revenue_agents.agents.base import Agent
from revenue_agents.crm import CRM
from revenue_agents.guardrails.gateway import ActionResult, Outcome
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.guardrails.untrusted import UNTRUSTED_RULES, injection_signals, wrap_untrusted
from revenue_agents.models import Deal, DealFields, Lead, Lesson, Model
from revenue_agents.runtime import AgentRun, AgentTask, NoInput, Script, Tool, ToolResult
from revenue_agents.segments import SegmentRegistry
from revenue_agents.stages import PROPOSAL_FIELDS
from revenue_agents.storage import utc_now

MAX_LESSONS = {"segment": 3, "global": 2}

SYSTEM_PROMPT = f"""You turn sales call transcripts into structured deal data for Cool AI, an AI advertising platform.

{UNTRUSTED_RULES}

Work with your tools: read the transcript and the deal, then save the deal fields with update_deal_fields, then
record lessons with record_lesson.
Extract only what was actually said. Leave a field empty if it wasn't discussed; never guess.
- budget_usd: a whole number of US dollars.
- products: use the exact product names read_deal lists.
- flight_start / flight_end: ISO dates (YYYY-MM-DD) for when the campaign runs.
- decision_maker: who signs the contract, as "Name (Title)".
List the customer's pains and objections in a few words each.
Lessons: up to three reusable, one-sentence insights about selling to this kind of customer (scope "segment"), not
facts about this one deal. An insight that would help sell to any kind of customer, e.g. how to handle a contract,
budget or timing objection, has scope "global" (up to two). Never record the same insight twice.
update_deal_fields tells you which fields it accepted and what is still missing for a proposal.
When you are done, reply with one short sentence and stop."""


class CallDigest(Model):
    """The scripted model's reading of a call (the real model writes the same things through its tools)."""

    deal_fields: DealFields
    pains: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list, max_length=3)
    general_lessons: list[str] = Field(default_factory=list, max_length=2)  # apply to every segment


class DealUpdate(DealFields):
    pains: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)


class LessonInput(Model):
    text: str = Field(max_length=300)
    scope: Literal["segment", "global"] = "segment"


class CallIntelAgent(Agent):
    name = "call_intel"

    def __init__(self, crm: CRM, segments: SegmentRegistry) -> None:
        self._crm = crm
        self._segments = segments

    def task(self, transcript_id: str, transcript: str, lead: Lead, deal: Deal) -> AgentTask:
        products = self._segments.get(lead.segment).products
        signals = injection_signals(transcript)
        counts = {"segment": 0, "global": 0}

        def read_transcript(_: NoInput) -> ToolResult:
            return ToolResult(text=wrap_untrusted(transcript, f"transcript:{transcript_id}"))

        def read_deal(_: NoInput) -> ToolResult:
            current = self._crm.get_deal(deal.id)
            known = {name: value for name, value in current.fields.model_dump(mode="json").items() if value}
            return ToolResult(text=(f"Deal {current.id} ({lead.company}), stage {current.stage.value}.\n"
                                    f"Products you may choose from: {'; '.join(products)}\n"
                                    f"Fields known so far: {known or 'none'}\n"
                                    f"A proposal needs: {', '.join(PROPOSAL_FIELDS)}"))

        def update(args: DealUpdate):
            fields, flags = check_fields(DealFields.model_validate(args.model_dump(exclude={"pains", "objections"})),
                                         products)
            if signals:
                flags.append("possible_prompt_injection")
            return self.propose(ActionType.UPDATE_DEAL, deal, f"call:{transcript_id}",
                                summary=f"Deal fields from call {transcript_id}",
                                payload={"fields": fields.model_dump(mode="json"), "pains": args.pains,
                                         "objections": args.objections, "flags": flags})

        def updated(args: DealUpdate, result: ActionResult) -> ToolResult:
            current = self._crm.get_deal(deal.id)
            missing = [name for name in PROPOSAL_FIELDS if not getattr(current.fields, name)]
            flags = [flag for flag in current.flags if flag in ("unknown_product_mentioned", "check_flight_dates")]
            return ToolResult(text=(f"Saved. Still missing for a proposal: {', '.join(missing) or 'nothing'}."
                                    + (f" Code flagged: {', '.join(flags)}." if flags else "")),
                              data={"missing": missing})

        def lesson_rule(args: LessonInput) -> str | None:
            if signals:
                return "This transcript contains instructions aimed at an AI: nothing is learned from it"
            if counts[args.scope] >= MAX_LESSONS[args.scope]:
                return f"At most {MAX_LESSONS[args.scope]} {args.scope} lessons per call"
            return None

        def lesson(args: LessonInput):
            number = counts[args.scope]
            lesson_id = f"lesson:{transcript_id}:" + (f"g{number}" if args.scope == "global" else str(number))
            item = Lesson(id=lesson_id, segment=lead.segment, source="call", source_id=transcript_id,
                          text=args.text.strip(), created_at=utc_now(), scope=args.scope)
            return self.propose(ActionType.RECORD_LESSON, deal, lesson_id, summary=f"Lesson: {item.text}",
                                payload={"lesson": item.model_dump(mode="json")})

        def lesson_saved(args: LessonInput, result: ActionResult) -> ToolResult:
            counts[args.scope] += 1
            return ToolResult(text=f"Lesson saved ({args.scope}).")

        def script(s: Script) -> None:
            s.call("read_transcript")
            s.call("read_deal")
            digest = s.ask(CallDigest)
            s.call("update_deal_fields", **digest.deal_fields.model_dump(mode="json"), pains=digest.pains,
                   objections=digest.objections)
            for text in digest.lessons:
                s.call("record_lesson", text=text, scope="segment")
            for text in digest.general_lessons:
                s.call("record_lesson", text=text, scope="global")
            s.say(f"Call {transcript_id} read.")

        tools = [
            Tool("read_transcript", "Read the call transcript.", NoInput, run=read_transcript),
            Tool("read_deal", "Read the deal's fields so far, the products you may choose from, and what a proposal "
                              "needs.", NoInput, run=read_deal),
            Tool("update_deal_fields", "Save what the call settled. Code checks products and dates before the deal "
                                       "changes; empty fields never erase what's already known.",
                 DealUpdate, action=update, done=updated, proposes=ActionType.UPDATE_DEAL),
            Tool("record_lesson", "Keep a reusable, one-sentence insight (scope segment or global).", LessonInput,
                 action=lesson, done=lesson_saved, check=lesson_rule, proposes=ActionType.RECORD_LESSON),
        ]
        return AgentTask(agent=self.name, title=f"Read call {transcript_id} ({lead.company})", deal_id=deal.id,
                         system=SYSTEM_PROMPT,
                         prompt=(f"Company: {lead.company} (segment: {lead.segment})\nThe transcript "
                                 f"{transcript_id} is ready. Read it and update the deal."),
                         tools=tools, script=script, max_turns=14)


def saved_fields(run: AgentRun) -> DealFields | None:
    """The deal fields a run saved (as checked by code), if it saved any."""
    for call in run.called("update_deal_fields"):
        if call.action is not None and call.outcome in (Outcome.EXECUTED, Outcome.DUPLICATE):
            return DealFields.model_validate(call.action.payload["fields"])
    return None


def check_fields(fields: DealFields, allowed_products: list[str]) -> tuple[DealFields, list[str]]:
    """Validate extracted fields against the segment pack and common sense."""
    flags = []
    canonical = {product.lower(): product for product in allowed_products}
    products = [canonical[p.lower()] for p in fields.products if p.lower() in canonical]
    if len(products) != len(fields.products):
        flags.append("unknown_product_mentioned")

    updates: dict = {"products": products}
    if fields.flight_start and fields.flight_end and fields.flight_end <= fields.flight_start:
        flags.append("check_flight_dates")
        updates.update(flight_start=None, flight_end=None)

    checked = fields.model_copy(update=updates)
    flags += [f"missing_{name}" for name in PROPOSAL_FIELDS if not getattr(checked, name)]
    return checked, flags
