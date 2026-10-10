"""Call intel agent: turn a call transcript (e.g. from Fireflies) into deal fields and lessons.

The LLM extracts. Plain code then checks the result before it can update the
deal: products must exist in the segment pack, flight dates must make sense,
and gaps that block a proposal are flagged.
"""

from __future__ import annotations

from pydantic import Field

from revenue_agents.agents.base import Agent, AgentResult
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.guardrails.untrusted import UNTRUSTED_RULES, injection_signals, wrap_untrusted
from revenue_agents.llm import LLM
from revenue_agents.models import Deal, DealFields, Lead, Lesson, Model
from revenue_agents.segments import SegmentRegistry
from revenue_agents.storage import utc_now

# Fields a proposal / IO can't be drafted without.
REQUIRED_FOR_PROPOSAL = ("budget_usd", "products", "flight_start", "flight_end", "decision_maker")

SYSTEM_PROMPT = f"""You turn sales call transcripts into structured deal data for Cool AI, an AI advertising platform.

{UNTRUSTED_RULES}

Extract only what was actually said. Leave a field empty if it wasn't discussed; never guess.
- budget_usd: a whole number of US dollars.
- products: use the exact product names from the list you are given.
- flight_start / flight_end: ISO dates (YYYY-MM-DD) for when the campaign runs.
- decision_maker: who signs the contract, as "Name (Title)".
List the customer's pains and objections in a few words each.
Write up to three lessons: reusable, one-sentence insights about selling to this kind of customer
(not facts about this one deal)."""


class CallDigest(Model):
    deal_fields: DealFields
    pains: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list, max_length=3)


class CallIntel(Model):
    transcript_id: str
    fields: DealFields
    pains: list[str]
    objections: list[str]
    flags: list[str]
    lessons: list[Lesson]


class CallIntelAgent(Agent):
    name = "call_intel"

    def __init__(self, llm: LLM, segments: SegmentRegistry) -> None:
        self._llm = llm
        self._segments = segments

    def run(self, transcript_id: str, transcript: str, lead: Lead, deal: Deal) -> AgentResult[CallIntel]:
        products = self._segments.get(lead.segment).products
        prompt = (
            f"Company: {lead.company} (segment: {lead.segment})\n"
            f"Products you may choose from: {'; '.join(products)}\n\n"
            f"Transcript:\n{wrap_untrusted(transcript, f'transcript:{transcript_id}')}"
        )
        digest = self._llm.complete(system=SYSTEM_PROMPT, prompt=prompt, schema=CallDigest)

        fields, flags = _check(digest.deal_fields, products)
        signals = injection_signals(transcript)
        if signals:
            flags.append("possible_prompt_injection")

        lessons = [] if signals else [
            Lesson(id=f"lesson:{transcript_id}:{i}", segment=lead.segment, source="call",
                   source_id=transcript_id, text=text, created_at=utc_now())
            for i, text in enumerate(digest.lessons)
        ]
        intel = CallIntel(transcript_id=transcript_id, fields=fields, pains=digest.pains,
                          objections=digest.objections, flags=flags, lessons=lessons)
        return AgentResult(output=intel, actions=self._actions(intel, deal))

    def _actions(self, intel: CallIntel, deal: Deal):
        actions = [
            self.propose(
                ActionType.UPDATE_DEAL, deal, f"call:{intel.transcript_id}",
                summary=f"Deal fields from call {intel.transcript_id}",
                payload={"fields": intel.fields.model_dump(mode="json"), "pains": intel.pains,
                         "objections": intel.objections, "flags": intel.flags},
            )
        ]
        actions += [
            self.propose(ActionType.RECORD_LESSON, deal, lesson.id, summary=f"Lesson: {lesson.text}",
                         payload={"lesson": lesson.model_dump(mode="json")})
            for lesson in intel.lessons
        ]
        return actions


def _check(fields: DealFields, allowed_products: list[str]) -> tuple[DealFields, list[str]]:
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
    flags += [f"missing_{name}" for name in REQUIRED_FOR_PROPOSAL if not getattr(checked, name)]
    return checked, flags
