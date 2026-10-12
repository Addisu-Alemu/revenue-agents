"""Outreach agent: draft a first-touch email from the segment pack and past lessons.

The LLM writes only a short personal opening and picks one of the pack's value
props. The email itself comes from the pack's template, so structure, tone
and call to action stay under config control. Plain code then checks the
draft: phrases the pack forbids are rejected (one retry with feedback), and
only lessons that were actually offered can be cited. If lessons were offered
but none was applied, the model is asked once more; it never blocks the email.

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
from revenue_agents.models import Deal, Enrichment, Lead, Lesson, Model, Reply
from revenue_agents.segments import SegmentPack, SegmentRegistry

MAX_ATTEMPTS = 2

# The template already says "Hi {first_name},". If the model greets anyway, the greeting is removed.
GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|dear|greetings|good (?:morning|afternoon|evening))\b(?:\s+[A-Z][\w.'-]*){0,3}\s*[,!:—-]\s*",
    re.IGNORECASE,
)

SYSTEM_PROMPT = f"""You write the opening of first-touch sales emails for Cool AI, an AI advertising platform.

{UNTRUSTED_RULES}

The email is: greeting, your opening, the value proposition you pick (on its own line), a call to action, signature.
Write `opening`: one or two sentences, specific to this company, based on its facts and buying signals.
The opening says why you're writing to them now. Don't describe what Cool AI does or restate a value
proposition: the line right after your opening already does that.
Don't start with a greeting (like "Hi Maya,") and don't sign off: the email template adds both.
Pick `value_prop_index`: the number of the value proposition that fits this company best.
Follow the tone you are given. No pricing, discounts or promises.
Lessons: if you are given any, pick the one most relevant to this company, let it shape your opening or
your value proposition choice, and list its id in `lessons_used`. List only lessons you actually applied.
If a colleague reviewed a previous draft, their feedback comes first: follow it."""

ANSWER_PROMPT = f"""You answer a prospect's question by email for Cool AI, an AI advertising platform.

{UNTRUSTED_RULES}

The email is: greeting, your answer, an invitation to a call, signature. The template adds the greeting, the
invitation and the signature, so write only `answer`: two to four sentences that answer the question directly.
Use only what the value propositions and products you are given say. Don't invent features, numbers,
timelines, customers or integrations. No pricing, discounts or promises.
If a lesson you are given helped, list its id in `lessons_used`. List only lessons you actually applied."""

# Sent once if lessons were offered but none was applied. It nudges; it never blocks the email.
LESSON_NOTE = ("\n\nYou were given lessons from past deals but applied none. Pick the one most relevant to this "
               "company, apply it, and list its id in `lessons_used`.")


class OutreachDraft(Model):
    opening: str = Field(max_length=400)
    value_prop_index: int
    lessons_used: list[str] = Field(default_factory=list)


class AnswerDraft(Model):
    answer: str = Field(max_length=700)
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
        relevant = [lesson for lesson in lessons if lesson.applies_in(lead.segment) and "outreach" in lesson.applies_to]
        prompt = _prompt(lead, enrichment, pack, relevant) + (review.as_prompt() if review else "")
        version = review.next_version if review else 1

        retry_note = ""
        fixes_left = MAX_ATTEMPTS - 1
        lesson_nudge_left = bool(relevant)
        while True:
            draft = self._llm.complete(system=SYSTEM_PROMPT, prompt=prompt + retry_note, schema=OutreachDraft)
            email, problems = self._render(lead, pack, draft, {lesson.id for lesson in relevant})
            if problems:
                if not fixes_left:
                    raise DraftRejectedError(f"No acceptable draft for {lead.company}: {'; '.join(problems)}")
                fixes_left -= 1
                retry_note = "\n\nYour previous draft was rejected: " + "; ".join(problems) + ". Fix this."
                continue
            if lesson_nudge_left and not email.lessons_used:
                lesson_nudge_left = False
                retry_note = LESSON_NOTE
                continue

            action = self.propose(
                ActionType.SEND_EMAIL, deal, key="first_touch" if version == 1 else f"first_touch:v{version}",
                summary=f"First email to {lead.contact.name} at {lead.company}"
                        + (f" (draft v{version}, after review)" if version > 1 else ""),
                payload=email.model_dump(mode="json"),
            )
            return AgentResult(output=email, actions=[action])

    def answer(self, lead: Lead, deal: Deal, reply: Reply, question: str, lessons: list[Lesson]) -> AgentResult[Email]:
        """Answer a prospect's question about the product, then invite them to a call. Proposes `send_email`."""
        pack = self._segments.get(lead.segment)
        relevant = [lesson for lesson in lessons if lesson.applies_in(lead.segment) and "outreach" in lesson.applies_to]
        prompt = _answer_prompt(lead, pack, reply, question, relevant)
        first_name = lead.contact.name.split()[0]
        subject = reply.subject if reply.subject.lower().startswith("re:") else f"Re: {reply.subject}"

        retry_note = ""
        for _ in range(MAX_ATTEMPTS):
            draft = self._llm.complete(system=ANSWER_PROMPT, prompt=prompt + retry_note, schema=AnswerDraft)
            body = (f"Hi {first_name},\n\n{_without_greeting(draft.answer)}\n\n"
                    f"Would you be open to {pack.messaging.call_to_action}?\n\nBest,\n{self._sender_name}")
            banned = _forbidden_phrases(body, pack.messaging.avoid_phrases)
            if not banned:
                offered = {lesson.id for lesson in relevant}
                email = Email(to=lead.contact.email, subject=subject, body=body, value_prop="",
                              lessons_used=[lesson_id for lesson_id in draft.lessons_used if lesson_id in offered])
                action = self.propose(ActionType.SEND_EMAIL, deal, key=f"reply:{reply.id}",
                                      summary=f"Answer to {lead.contact.name}'s question at {lead.company}",
                                      payload=email.model_dump(mode="json"))
                return AgentResult(output=email, actions=[action])
            retry_note = f"\n\nYour previous answer was rejected: it uses forbidden phrases {banned}. Fix this."
        raise DraftRejectedError(f"No acceptable answer for {lead.company}: forbidden phrases {banned}")

    def _render(self, lead: Lead, pack: SegmentPack, draft: OutreachDraft, offered: set[str]) -> tuple[Email, list[str]]:
        problems = []
        if not 0 <= draft.value_prop_index < len(pack.messaging.value_props):
            problems.append(f"value_prop_index must be 0-{len(pack.messaging.value_props) - 1}")
            value_prop = pack.messaging.value_props[0]
        else:
            value_prop = pack.messaging.value_props[draft.value_prop_index]

        first_name = lead.contact.name.split()[0]
        opening = _without_greeting(draft.opening)
        subject = pack.templates.first_touch_subject.format(company=lead.company, first_name=first_name)
        body = pack.templates.first_touch_body.format(
            company=lead.company, first_name=first_name, opening=opening, value_prop=value_prop,
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


def _answer_prompt(lead: Lead, pack: SegmentPack, reply: Reply, question: str, lessons: list[Lesson]) -> str:
    value_props = "\n".join(f"- {text}" for text in pack.messaging.value_props)
    products = "\n".join(f"- {product}" for product in pack.products)
    lesson_lines = "\n".join(f"- [{lesson.id}] {lesson.text}" for lesson in lessons) or "- (none yet)"
    return (
        f"Company: {lead.company}\nContact: {lead.contact.name}, {lead.contact.title}\n"
        f"Tone: {pack.messaging.tone}\n\n"
        f"Value propositions (the only claims you may make):\n{value_props}\n\n"
        f"Products:\n{products}\n\n"
        f"Lessons learned from past calls and replies:\n{lesson_lines}\n\n"
        f"Their question, as our reply triage read it: {question}\n\n"
        f"Their email:\n{wrap_untrusted(reply.body, f'reply:{reply.id}')}"
    )


def _without_greeting(opening: str) -> str:
    """Safety net for the prompt rule: drop a leading "Hi Maya," and capitalise what's left."""
    text = GREETING.sub("", opening.strip(), count=1)
    return text[:1].upper() + text[1:] if text else opening.strip()


def _forbidden_phrases(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if re.search(rf"\b{re.escape(p)}\b", text, re.IGNORECASE)]
