"""Action handlers: what actually happens when the gateway executes an action.

This is the only code with side effects, and it only runs after the policy
said yes (or a human approved). Handlers record facts in the CRM and lesson
store. Deciding what happens next (stage changes) is the orchestrator's job.

Sending is mocked: an "email_sent" activity on the deal timeline is the outbox,
and IOs go to a mock e-signature provider.
"""

from __future__ import annotations

from typing import Any

from revenue_agents.crm import CRM
from revenue_agents.esign import ESignProvider
from revenue_agents.guardrails.gateway import Handler
from revenue_agents.guardrails.policy import Action, ActionType
from revenue_agents.lessons import LessonStore
from revenue_agents.models import DealFields, Enrichment, LeadScore, Lesson, Message
from revenue_agents.storage import utc_now

SENDER = "daniel.reyes@cool.example"


class ActionHandlers:
    def __init__(self, crm: CRM, lessons: LessonStore, esign: ESignProvider) -> None:
        self._crm = crm
        self._lessons = lessons
        self._esign = esign

    def as_dict(self) -> dict[ActionType, Handler]:
        return {
            ActionType.ENRICH_LEAD: self.enrich_lead,
            ActionType.SCORE_LEAD: self.score_lead,
            ActionType.RECORD_RESEARCH: self.record_research,
            ActionType.UPDATE_DEAL: self.update_deal,
            ActionType.FLAG_DEAL: self.flag_deal,
            ActionType.CLASSIFY_REPLY: self.classify_reply,
            ActionType.RECORD_LESSON: self.record_lesson,
            ActionType.SEND_EMAIL: self.send_email,
            ActionType.BOOK_MEETING: self.book_meeting,
            ActionType.SCHEDULE_FOLLOW_UP: self.schedule_follow_up,
            ActionType.PREPARE_CALL_BRIEF: self.prepare_call_brief,
            ActionType.DRAFT_IO: self.draft_io,
            ActionType.SEND_IO: self.send_io,
            ActionType.VOID_IO: self.void_io,
            ActionType.CLOSE_DEAL: self.close_deal,
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

    def record_research(self, action: Action) -> None:
        """The research agent's note: why this company might buy now, and what to lead with."""
        self._crm.log(action.deal_id, "research", action.summary,
                      {"why_now": action.payload["why_now"], "angle": action.payload["angle"]})

    def update_deal(self, action: Action) -> None:
        """Merge new facts into the deal. Empty values never erase what's already known."""
        deal = self._crm.get_deal(action.deal_id)
        merged = deal.fields.model_dump()
        merged.update({key: value for key, value in action.payload.get("fields", {}).items() if value not in (None, "", [])})
        fields = DealFields.model_validate(merged)
        flags = _merge_flags(deal.flags, action.payload.get("flags", []))
        # A later call can answer what an earlier one left open: drop "missing_<field>" once the field is known.
        flags = [flag for flag in flags if not (flag.startswith("missing_") and getattr(fields, flag[8:], None))]
        flags = [flag for flag in flags if flag not in action.payload.get("clear_flags", [])]  # e.g. a change resolved
        updates: dict[str, Any] = {"fields": fields, "flags": flags}
        self._crm.save_deal(deal.model_copy(update=updates))
        self._crm.log(deal.id, "note", action.summary,
                      {key: action.payload.get(key, []) for key in ("pains", "objections")})

    def flag_deal(self, action: Action) -> None:
        deal = self._crm.get_deal(action.deal_id)
        flags = action.payload.get("flags", [])
        self._crm.save_deal(deal.model_copy(update={"flags": _merge_flags(deal.flags, flags)}))
        self._crm.log(deal.id, "flag", action.summary, action.payload)

    def classify_reply(self, action: Action) -> None:
        """Record how the reply was read, and put its flags (set in code, not by the model) on the deal."""
        deal = self._crm.get_deal(action.deal_id)
        flags = action.payload.get("flags", [])
        if flags:
            self._crm.save_deal(deal.model_copy(update={"flags": _merge_flags(deal.flags, flags)}))
        self._crm.log(action.deal_id, "reply", action.summary, action.payload)

    def record_lesson(self, action: Action) -> bool:
        lesson = Lesson.model_validate(action.payload["lesson"])
        added = self._lessons.add(lesson)
        self._crm.log(action.deal_id, "note", action.summary, {"lesson_id": lesson.id})
        return added

    def send_email(self, action: Action) -> dict[str, str]:
        """Send (mock outbox) and store the email in the deal's conversation, so replies can be linked to it."""
        email = action.payload
        message = Message(id=f"msg:{action.id}", thread_id=f"T-{action.deal_id}", deal_id=action.deal_id,
                          direction="outbound", sender=SENDER, to=email["to"], subject=email["subject"],
                          body=email["body"], at=utc_now(),
                          meta={"action_id": action.id, "lessons_used": email.get("lessons_used", []),
                                "edited_by": email.get("edited_by")})
        self._crm.add_message(message)
        self._crm.log(action.deal_id, "email_sent", f"Email to {email['to']}: {email['subject']}",
                      {**email, "message_id": message.id, "thread_id": message.thread_id})
        self._lessons.record_use(email.get("lessons_used", []), used_in=action.id, deal_id=action.deal_id)
        return {"status": "sent (mock)", "to": email["to"], "message_id": message.id}

    def book_meeting(self, action: Action) -> dict[str, str]:
        """Put the meeting the prospect asked for on the rep's calendar (mocked: an entry on the deal timeline)."""
        payload = action.payload
        self._crm.log(action.deal_id, "meeting_booked", action.summary,
                      {"when": payload["when"], "attendees": payload.get("attendees", []),
                       "note": payload.get("note", ""), "reply_id": payload.get("reply_id")})
        return {"status": "booked (mock calendar)", "when": payload["when"]}

    def schedule_follow_up(self, action: Action) -> dict[str, str]:
        """A timer on the deal: on that date the rep is reminded to follow up (the scheduler fires it)."""
        payload = action.payload
        self._crm.log(action.deal_id, "follow_up", action.summary,
                      {"due": payload["due"], "reason": payload["reason"], "reply_id": payload.get("reply_id")})
        return {"status": "scheduled", "due": payload["due"]}

    def prepare_call_brief(self, action: Action) -> None:
        self._crm.log(action.deal_id, "call_brief", action.summary,
                      {"brief": action.payload["brief"], "markdown": action.payload["markdown"]})
        self._lessons.record_use(action.payload.get("lessons_used", []), used_in=action.id, deal_id=action.deal_id)

    def draft_io(self, action: Action) -> None:
        payload = action.payload
        self._crm.log(action.deal_id, "document", action.summary,
                      {"io_number": payload["io_number"], "document": payload["document"],
                       "version": payload.get("version", 1), "terms": payload.get("terms", {})})

    def send_io(self, action: Action) -> dict[str, str]:
        """Send the IO for e-signature. The envelope id ties the later "signed" webhook to this deal."""
        payload = action.payload
        envelope = self._esign.send(action.deal_id, payload["io_number"], payload["document"], payload["to"])
        self._crm.log(action.deal_id, "io_sent", f"{action.summary} for e-signature ({envelope.id})",
                      {"io_number": payload["io_number"], "to": payload["to"], "envelope_id": envelope.id})
        return {"status": "sent for e-signature (mock)", "envelope_id": envelope.id}

    def void_io(self, action: Action) -> dict[str, str]:
        """Withdraw an outdated IO at the e-sign provider, so the customer can't sign it."""
        envelope = self._esign.void(action.payload["envelope_id"], action.payload["reason"])
        self._crm.log(action.deal_id, "io_voided", action.summary,
                      {"envelope_id": envelope.id, "io_number": envelope.io_number, "reason": envelope.void_reason})
        return {"status": "voided", "envelope_id": envelope.id}

    def close_deal(self, action: Action) -> None:
        """Closed won: write the handoff note the delivery team starts from."""
        deal = self._crm.get_deal(action.deal_id)
        fields = deal.fields
        handoff = {
            "products": fields.products, "budget_usd": fields.budget_usd, "scope": fields.scope,
            "flight_start": fields.flight_start.isoformat() if fields.flight_start else None,
            "flight_end": fields.flight_end.isoformat() if fields.flight_end else None,
            "signed_by": action.payload.get("signed_by"), "envelope_id": action.payload.get("envelope_id"),
            "special_terms": fields.special_terms,
        }
        self._crm.log(deal.id, "handoff", action.summary, handoff)


def _merge_flags(existing: list[str], new: list[str]) -> list[str]:
    return existing + [flag for flag in new if flag not in existing]
