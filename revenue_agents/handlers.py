"""Action handlers: what actually happens when the gateway executes an action.

This is the only code with side effects, and it only runs after the policy
said yes (or a human approved). Handlers record facts in the CRM and lesson
store. Deciding what happens next (stage changes) is the orchestrator's job.

Sending is mocked: an "email_sent" activity on the deal timeline is the outbox.
"""

from __future__ import annotations

from typing import Any

from revenue_agents.crm import CRM
from revenue_agents.guardrails.gateway import Handler
from revenue_agents.guardrails.policy import Action, ActionType
from revenue_agents.lessons import LessonStore
from revenue_agents.models import DealFields, Enrichment, LeadScore, Lesson


class ActionHandlers:
    def __init__(self, crm: CRM, lessons: LessonStore) -> None:
        self._crm = crm
        self._lessons = lessons

    def as_dict(self) -> dict[ActionType, Handler]:
        return {
            ActionType.ENRICH_LEAD: self.enrich_lead,
            ActionType.SCORE_LEAD: self.score_lead,
            ActionType.UPDATE_DEAL: self.update_deal,
            ActionType.FLAG_DEAL: self.flag_deal,
            ActionType.CLASSIFY_REPLY: self.classify_reply,
            ActionType.RECORD_LESSON: self.record_lesson,
            ActionType.SEND_EMAIL: self.send_email,
            ActionType.DRAFT_IO: self.draft_io,
            ActionType.SEND_IO: self.send_io,
        }

    def enrich_lead(self, action: Action) -> None:
        deal = self._crm.get_deal(action.deal_id)
        enrichment = Enrichment.model_validate(action.payload["enrichment"])
        self._crm.save_deal(deal.model_copy(update={"enrichment": enrichment}))
        self._crm.log(deal.id, "note", action.summary)

    def score_lead(self, action: Action) -> None:
        deal = self._crm.get_deal(action.deal_id)
        score = LeadScore.model_validate(action.payload["score"])
        self._crm.save_deal(deal.model_copy(update={"score": score}))
        self._crm.log(deal.id, "note", action.summary, {"score": action.payload["score"]})

    def update_deal(self, action: Action) -> None:
        """Merge new facts into the deal. Empty values never erase what's already known."""
        deal = self._crm.get_deal(action.deal_id)
        merged = deal.fields.model_dump()
        merged.update({key: value for key, value in action.payload.get("fields", {}).items() if value not in (None, "", [])})
        updates: dict[str, Any] = {"fields": DealFields.model_validate(merged),
                                   "flags": _merge_flags(deal.flags, action.payload.get("flags", []))}
        self._crm.save_deal(deal.model_copy(update=updates))
        self._crm.log(deal.id, "note", action.summary,
                      {key: action.payload.get(key, []) for key in ("pains", "objections")})

    def flag_deal(self, action: Action) -> None:
        deal = self._crm.get_deal(action.deal_id)
        flags = action.payload.get("flags", [])
        self._crm.save_deal(deal.model_copy(update={"flags": _merge_flags(deal.flags, flags)}))
        self._crm.log(deal.id, "flag", action.summary, action.payload)

    def classify_reply(self, action: Action) -> None:
        self._crm.log(action.deal_id, "reply", action.summary, action.payload)

    def record_lesson(self, action: Action) -> bool:
        lesson = Lesson.model_validate(action.payload["lesson"])
        added = self._lessons.add(lesson)
        self._crm.log(action.deal_id, "note", action.summary, {"lesson_id": lesson.id})
        return added

    def send_email(self, action: Action) -> dict[str, str]:
        email = action.payload
        self._crm.log(action.deal_id, "email_sent", f"Email to {email['to']}: {email['subject']}", email)
        self._lessons.record_use(email.get("lessons_used", []), used_in=action.id, deal_id=action.deal_id)
        return {"status": "sent (mock)", "to": email["to"]}

    def draft_io(self, action: Action) -> None:
        self._crm.log(action.deal_id, "document", action.summary,
                      {"io_number": action.payload["io_number"], "document": action.payload["document"]})

    def send_io(self, action: Action) -> dict[str, str]:
        self._crm.log(action.deal_id, "io_sent", action.summary,
                      {"io_number": action.payload["io_number"], "to": action.payload["to"]})
        return {"status": "sent for review (mock)", "io_number": action.payload["io_number"]}


def _merge_flags(existing: list[str], new: list[str]) -> list[str]:
    return existing + [flag for flag in new if flag not in existing]
