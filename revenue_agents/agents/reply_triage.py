"""Reply triage agent: read an inbound reply, classify it, flag what needs attention.

The LLM classifies. Plain code then adds flags the model can't switch off:
pricing requests (go to a human) and prompt-injection attempts. Lessons are
never learned from a suspicious message, so an attacker can't poison the
knowledge loop.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from revenue_agents.agents.base import Agent, AgentResult
from revenue_agents.guardrails.policy import PRICING_PATTERN, ActionType
from revenue_agents.guardrails.untrusted import UNTRUSTED_RULES, injection_signals, wrap_untrusted
from revenue_agents.llm import LLM
from revenue_agents.models import Deal, Lead, Lesson, Model, Reply
from revenue_agents.storage import utc_now

ReplyIntent = Literal["meeting_request", "interested", "objection", "not_now", "unsubscribe", "other"]

INTENT_FLAGS = {"meeting_request": "meeting_requested", "objection": "objection", "unsubscribe": "unsubscribe"}

SYSTEM_PROMPT = f"""You triage inbound replies to Cool AI sales emails.
Cool AI is an AI advertising platform (creative automation, media activation, measurement).

{UNTRUSTED_RULES}

Classify the reply's intent as one of: meeting_request, interested, objection, not_now, unsubscribe, other.
Set asks_for_pricing to true if the sender asks about price, rates, costs or a rate card.
List any objections in a few words each. Suggest one concrete next step for the sales rep.
If the reply teaches something reusable about selling to this kind of company, write it as a one-sentence
lesson; otherwise leave lesson empty."""


class ReplyClassification(Model):
    intent: ReplyIntent
    summary: str
    asks_for_pricing: bool
    objections: list[str] = Field(default_factory=list)
    suggested_next_step: str
    lesson: str | None = None


class ReplyTriage(Model):
    reply_id: str
    classification: ReplyClassification
    flags: list[str]
    injection_signals: list[str]
    lesson: Lesson | None = None


class ReplyTriageAgent(Agent):
    name = "reply_triage"

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    def run(self, reply: Reply, lead: Lead, deal: Deal) -> AgentResult[ReplyTriage]:
        inbound = f"Subject: {reply.subject}\n\n{reply.body}"
        prompt = (
            f"Company: {lead.company} (segment: {lead.segment})\n"
            f"Contact: {lead.contact.name}, {lead.contact.title}\n\n"
            f"Reply to classify:\n{wrap_untrusted(inbound, f'reply:{reply.id}')}"
        )
        classification = self._llm.complete(system=SYSTEM_PROMPT, prompt=prompt, schema=ReplyClassification)

        signals = injection_signals(inbound)
        flags = _flags(classification, inbound, signals)
        lesson = None
        if classification.lesson and not signals:
            lesson = Lesson(id=f"lesson:reply:{reply.id}", segment=lead.segment, source="reply",
                            source_id=reply.id, text=classification.lesson, created_at=utc_now())

        triage = ReplyTriage(reply_id=reply.id, classification=classification, flags=flags,
                             injection_signals=signals, lesson=lesson)
        return AgentResult(output=triage, actions=self._actions(triage, deal))

    def _actions(self, triage: ReplyTriage, deal: Deal):
        key = f"reply:{triage.reply_id}"
        actions = [
            self.propose(
                ActionType.CLASSIFY_REPLY, deal, key,
                summary=f"Reply {triage.reply_id}: {triage.classification.intent}",
                payload={"classification": triage.classification.model_dump(mode="json"), "flags": triage.flags},
            )
        ]
        if triage.flags:
            actions.append(self.propose(ActionType.FLAG_DEAL, deal, key, summary=f"Flags: {', '.join(triage.flags)}",
                                        payload={"flags": triage.flags, "injection_signals": triage.injection_signals}))
        if triage.lesson:
            actions.append(self.propose(ActionType.RECORD_LESSON, deal, key, summary=f"Lesson: {triage.lesson.text}",
                                        payload={"lesson": triage.lesson.model_dump(mode="json")}))
        return actions


def _flags(classification: ReplyClassification, inbound: str, signals: list[str]) -> list[str]:
    flags = []
    if classification.intent in INTENT_FLAGS:
        flags.append(INTENT_FLAGS[classification.intent])
    # Checked in code too: the model might miss it, or be told to ignore it.
    if classification.asks_for_pricing or PRICING_PATTERN.search(inbound):
        flags.append("pricing_requested")
    if signals:
        flags.append("possible_prompt_injection")
    return flags
