"""Outreach agent: draft a first-touch email from the segment pack and past lessons.

The LLM writes only a short personal opening and picks one of the pack's value
props. The email itself comes from the pack's template, so structure, tone
and call to action stay under config control. Plain code then checks the
draft: phrases the pack forbids are rejected (one retry with feedback), and
only lessons that were actually offered can be cited.

The agent proposes `send_email`. Whether it goes out automatically or waits
for a human is the policy's decision (Tier A first touch waits). If a human
rejects a draft with a note, the agent writes the next version with that
note as reviewer feedback.
"""

from __future__ import annotations

import re

from pydantic import Field

from revenue_agents.agents.base import Agent, AgentResult
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.guardrails.untrusted import UNTRUSTED_RULES, wrap_untrusted
from revenue_agents.llm import LLM
from revenue_agents.models import Deal, Enrichment, Lead, Lesson, Model
from revenue_agents.segments import SegmentPack, SegmentRegistry

MAX_ATTEMPTS = 2

SYSTEM_PROMPT = f"""You write the opening of first-touch sales emails for Cool AI, an AI advertising platform.

{UNTRUSTED_RULES}

Write `opening`: one or two sentences, specific to this company, based on its facts and buying signals.
Pick `value_prop_index`: the number of the value proposition that fits this company best.
Follow the tone you are given. No pricing, discounts or promises.
If any of the lessons you are given shaped your choice, list their ids in `lessons_used`.
If a colleague reviewed a previous draft, their feedback comes first: follow it."""


class OutreachDraft(Model):
    opening: str = Field(max_length=400)
    value_prop_index: int
    lessons_used: list[str] = Field(default_factory=list)


class Email(Model):
    to: str
    subject: str
    body: str
    value_prop: str
    lessons_used: list[str]


class Review(Model):
    """A human rejected a draft and said why. The next draft must follow the feedback."""

    feedback: str
    previous_body: str
    next_version: int

    def as_prompt(self) -> str:
        return (f"\n\nA colleague reviewed your previous draft and rejected it. Their feedback (follow it):\n"
                f"{self.feedback}\n\nPrevious draft:\n{self.previous_body}")


class DraftRejectedError(ValueError):
    """The model couldn't produce an acceptable draft within the allowed attempts."""


class OutreachAgent(Agent):
    name = "outreach"

    def __init__(self, llm: LLM, segments: SegmentRegistry, sender_name: str = "Daniel Reyes") -> None:
        self._llm = llm
        self._segments = segments
        self._sender_name = sender_name

    def run(self, lead: Lead, deal: Deal, enrichment: Enrichment, lessons: list[Lesson],
            review: Review | None = None) -> AgentResult[Email]:
        pack = self._segments.get(lead.segment)
        relevant = [lesson for lesson in lessons if lesson.segment == lead.segment and "outreach" in lesson.applies_to]
        prompt = _prompt(lead, enrichment, pack, relevant) + (review.as_prompt() if review else "")
        version = review.next_version if review else 1

        retry_note = ""
        for _ in range(MAX_ATTEMPTS):
            draft = self._llm.complete(system=SYSTEM_PROMPT, prompt=prompt + retry_note, schema=OutreachDraft)
            email, problems = self._render(lead, pack, draft, {lesson.id for lesson in relevant})
            if not problems:
                action = self.propose(
                    ActionType.SEND_EMAIL, deal, key="first_touch" if version == 1 else f"first_touch:v{version}",
                    summary=f"First email to {lead.contact.name} at {lead.company}"
                            + (f" (draft v{version}, after review)" if version > 1 else ""),
                    payload=email.model_dump(mode="json"),
                )
                return AgentResult(output=email, actions=[action])
            retry_note = "\n\nYour previous draft was rejected: " + "; ".join(problems) + ". Fix this."

        raise DraftRejectedError(f"No acceptable draft for {lead.company}: {'; '.join(problems)}")

    def _render(self, lead: Lead, pack: SegmentPack, draft: OutreachDraft, offered: set[str]) -> tuple[Email, list[str]]:
        problems = []
        if not 0 <= draft.value_prop_index < len(pack.messaging.value_props):
            problems.append(f"value_prop_index must be 0-{len(pack.messaging.value_props) - 1}")
            value_prop = pack.messaging.value_props[0]
        else:
            value_prop = pack.messaging.value_props[draft.value_prop_index]

        first_name = lead.contact.name.split()[0]
        subject = pack.templates.first_touch_subject.format(company=lead.company, first_name=first_name)
        body = pack.templates.first_touch_body.format(
            company=lead.company, first_name=first_name, opening=draft.opening.strip(), value_prop=value_prop,
            call_to_action=pack.messaging.call_to_action, sender_name=self._sender_name,
        )

        banned = _forbidden_phrases(f"{subject}\n{body}", pack.messaging.avoid_phrases)
        if banned:
            problems.append(f"it uses forbidden phrases {banned}")

        email = Email(to=lead.contact.email, subject=subject, body=body, value_prop=value_prop,
                      lessons_used=[lesson_id for lesson_id in draft.lessons_used if lesson_id in offered])
        return email, problems


def _prompt(lead: Lead, enrichment: Enrichment, pack: SegmentPack, lessons: list[Lesson]) -> str:
    value_props = "\n".join(f"{i}. {text}" for i, text in enumerate(pack.messaging.value_props))
    lesson_lines = "\n".join(f"- [{lesson.id}] {lesson.text}" for lesson in lessons) or "- (none yet)"
    # Enrichment comes from an outside provider, so it's data, not instructions.
    facts = "\n".join(
        [f"Industry: {enrichment.industry}; model: {enrichment.business_model}; "
         f"{enrichment.locations} locations; {enrichment.employees} employees"]
        + [f"Signal ({signal.type}): {signal.detail}" for signal in enrichment.signals]
    )
    return (
        f"Company: {lead.company}\nContact: {lead.contact.name}, {lead.contact.title}\n"
        f"Ideal customer for this segment: {pack.icp_summary}\n"
        f"Tone: {pack.messaging.tone}\n\n"
        f"Value propositions:\n{value_props}\n\n"
        f"Lessons learned from past calls and replies:\n{lesson_lines}\n\n"
        f"Company facts:\n{wrap_untrusted(facts, f'enrichment:{lead.domain}')}"
    )


def _forbidden_phrases(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if re.search(rf"\b{re.escape(p)}\b", text, re.IGNORECASE)]
