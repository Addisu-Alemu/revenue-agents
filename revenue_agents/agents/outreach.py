"""Outreach agent: write and send a first-touch email, using the segment pack and what the team has learned.

The model works with tools: it reads the account (facts, the research note,
the pack's tone and value propositions) and the lessons, then drafts. It writes
only a short personal opening and picks one of the pack's value props: the
draft_email tool renders the pack's template, so structure, tone and call to
action stay under config control. Plain code then checks the draft (phrases
the pack forbids, lessons that were never offered) and hands problems back,
so the model fixes its own draft before it can send.

send_email proposes `send_email`. Whether it goes out on its own or waits for
a human is the policy's decision (Tier A first touch waits). If a human
rejects a draft with a note, a new run writes the next version with that note
as reviewer feedback.
"""

from __future__ import annotations

import re

from pydantic import Field

from revenue_agents.agents.base import Agent
from revenue_agents.crm import CRM
from revenue_agents.guardrails.gateway import ActionResult
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.guardrails.untrusted import UNTRUSTED_RULES, wrap_untrusted
from revenue_agents.lessons import LessonStore
from revenue_agents.models import Deal, Lead, Lesson, Model
from revenue_agents.runtime import AgentTask, NoInput, Script, Tool, ToolResult
from revenue_agents.segments import SegmentPack, SegmentRegistry

MAX_ATTEMPTS = 2

# The template already says "Hi {first_name},". If the model greets anyway, the greeting is removed.
GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|dear|greetings|good (?:morning|afternoon|evening))\b(?:\s+[A-Z][\w.'-]*){0,3}\s*[,!:—-]\s*",
    re.IGNORECASE,
)

SYSTEM_PROMPT = f"""You write first-touch sales emails for Cool AI, an AI advertising platform.

{UNTRUSTED_RULES}

Work with your tools: read the account, search the lessons, draft the email, fix it if the draft tool reports a
problem, then send it. Send only a draft the draft tool accepted.

The email is: greeting, your opening, the value proposition you pick (on its own line), a call to action, signature.
The template adds the greeting, the value proposition line, the call to action and the signature, so you write only
`opening`: one or two sentences, specific to this company, based on its facts, buying signals and the research note.
The opening says why you're writing to them now. Don't describe what Cool AI does or restate a value
proposition: the line right after your opening already does that.
Don't start with a greeting (like "Hi Maya,") and don't sign off: the template adds both.
Pick `value_prop_index`: the number of the value proposition that fits this company best.
Follow the tone you are given. No pricing, discounts or promises.
Lessons: if there are any, pick the one most relevant to this company, let it shape your opening or
your value proposition choice, and list its id in `lessons_used`. List only lessons you actually applied.
If a colleague reviewed a previous draft, their feedback comes first: follow it.
When the email is sent or waiting for approval, reply with one short sentence and stop."""

# Sent once if lessons were offered but none was applied. It nudges; it never blocks the email.
LESSON_NOTE = ("\n\nYou were given lessons from past deals but applied none. Pick the one most relevant to this "
               "company, apply it, and list its id in `lessons_used`.")


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


class OutreachAgent(Agent):
    name = "outreach"

    def __init__(self, crm: CRM, lessons: LessonStore, segments: SegmentRegistry,
                 sender_name: str = "Daniel Reyes") -> None:
        self._crm = crm
        self._lessons = lessons
        self._segments = segments
        self._sender_name = sender_name

    def first_touch(self, lead: Lead, deal: Deal, review: Review | None = None) -> AgentTask:
        pack = self._segments.get(lead.segment)
        relevant = [lesson for lesson in self._lessons.for_segment(lead.segment, purpose="outreach")
                    if lesson.applies_in(lead.segment) and "outreach" in lesson.applies_to]
        offered = {lesson.id for lesson in relevant}
        version = review.next_version if review else 1
        drafts: list[Email] = []

        def read_account(_: NoInput) -> ToolResult:
            return ToolResult(text=self._account(lead, deal.id, pack))

        def search_lessons(_: NoInput) -> ToolResult:
            lines = [f"- [{lesson.id}] {lesson.text} (from {lesson.source} {lesson.source_id})" for lesson in relevant]
            return ToolResult(text="Lessons learned from past calls and replies:\n" + ("\n".join(lines) or "- (none yet)"),
                              data={"lessons": [lesson.id for lesson in relevant]})

        def draft_email(args: OutreachDraft) -> ToolResult:
            email, problems = self._render(lead, pack, args, offered)
            if problems:
                return ToolResult(text="Draft rejected: " + "; ".join(problems) + ". Fix this and draft again.",
                                  data={"problems": problems}, is_error=True)
            drafts.append(email)
            note = (" You applied none of the lessons you were given: consider redrafting with the most relevant one."
                    if offered and not email.lessons_used else "")
            return ToolResult(text=f"Draft accepted.{note}\nSubject: {email.subject}\n\n{email.body}",
                              data={"email": email.model_dump(mode="json")})

        def no_draft(_: NoInput) -> str | None:
            return None if drafts else "Draft the email with draft_email first; only an accepted draft can be sent"

        def send(_: NoInput):
            email = drafts[-1]
            return self.propose(
                ActionType.SEND_EMAIL, deal, key="first_touch" if version == 1 else f"first_touch:v{version}",
                summary=f"First email to {lead.contact.name} at {lead.company}"
                        + (f" (draft v{version}, after review)" if version > 1 else ""),
                payload=email.model_dump(mode="json"),
            )

        def sent(_: NoInput, result: ActionResult) -> ToolResult:
            return ToolResult(text=f"Sent to {drafts[-1].to} (mock outbox).")

        def script(s: Script) -> None:
            s.call("read_account")
            s.call("search_lessons")
            note, fixes_left, nudge_left = "", MAX_ATTEMPTS - 1, bool(relevant)
            while True:
                result = s.call("draft_email", **s.ask(OutreachDraft, note).model_dump())
                if result.is_error:
                    if not fixes_left:
                        s.say(f"No acceptable draft for {lead.company}: {result.text}")
                        return
                    fixes_left -= 1
                    note = "\n\nYour previous draft was rejected: " + "; ".join(result.data["problems"]) + ". Fix this."
                    continue
                if nudge_left and not result.data["email"]["lessons_used"]:
                    nudge_left, note = False, LESSON_NOTE
                    continue
                s.call("send_email")
                s.say(f"First email to {lead.contact.name} sent.")
                return

        tools = [
            Tool("read_account", "Read what we know about the company: facts and buying signals, the research note, "
                                 "why it scored as it did, and the segment's tone and numbered value propositions.",
                 NoInput, run=read_account),
            Tool("search_lessons", "The lessons learned from past calls, replies and reviews that apply to this "
                                   "segment, with their ids.", NoInput, run=search_lessons),
            Tool("draft_email", "Render the email from the segment's template with your opening and value "
                                "proposition, and check it. Returns the full email, or the problems to fix.",
                 OutreachDraft, run=draft_email),
            Tool("send_email", "Send the last accepted draft. The policy decides whether it goes out now or waits "
                               "for a human.", NoInput, action=send, done=sent, check=no_draft,
                 proposes=ActionType.SEND_EMAIL),
        ]
        title = f"First email to {lead.contact.name} at {lead.company}" + (f" (v{version})" if version > 1 else "")
        prompt = _prompt(lead, pack) + (review.as_prompt() if review else "")
        return AgentTask(agent=self.name, title=title, deal_id=deal.id, system=SYSTEM_PROMPT, prompt=prompt,
                         tools=tools, script=script, max_turns=12)

    def _account(self, lead: Lead, deal_id: str, pack: SegmentPack) -> str:
        deal = self._crm.get_deal(deal_id)
        value_props = "\n".join(f"{i}. {text}" for i, text in enumerate(pack.messaging.value_props))
        notes = [a.data for a in self._crm.timeline(deal_id) if a.kind == "research"]
        research = (f"Why now: {notes[-1]['why_now']}\nAngle: {notes[-1]['angle']}" if notes
                    else "No research note.")
        facts = ["No company facts."]
        if deal.enrichment:
            e = deal.enrichment
            # Enrichment comes from an outside provider, so it's data, not instructions.
            facts = [wrap_untrusted("\n".join(
                [f"Industry: {e.industry}; model: {e.business_model}; {e.locations} locations; "
                 f"{e.employees} employees"]
                + [f"Signal ({signal.type}): {signal.detail}" for signal in e.signals]), f"enrichment:{lead.domain}")]
        score = (f"Tier {deal.score.tier.value} ({deal.score.total:g}): {deal.score.fit.reason}; {deal.score.intent.reason}"
                 if deal.score else "Not scored.")
        return (f"Ideal customer for this segment: {pack.icp_summary}\nTone: {pack.messaging.tone}\n"
                f"Never use: {', '.join(pack.messaging.avoid_phrases) or 'nothing listed'}\n\n"
                f"Value propositions:\n{value_props}\n\nScore: {score}\n\nResearch note:\n{research}\n\n"
                f"Company facts:\n{facts[0]}")

    def _render(self, lead: Lead, pack: SegmentPack, draft: OutreachDraft, offered: set[str]) -> tuple[Email, list[str]]:
        problems = []
        if not 0 <= draft.value_prop_index < len(pack.messaging.value_props):
            problems.append(f"value_prop_index must be 0-{len(pack.messaging.value_props) - 1}")
            value_prop = pack.messaging.value_props[0]
        else:
            value_prop = pack.messaging.value_props[draft.value_prop_index]

        first_name = lead.contact.name.split()[0]
        opening = without_greeting(draft.opening)
        subject = pack.templates.first_touch_subject.format(company=lead.company, first_name=first_name)
        body = pack.templates.first_touch_body.format(
            company=lead.company, first_name=first_name, opening=opening, value_prop=value_prop,
            call_to_action=pack.messaging.call_to_action, sender_name=self._sender_name,
        )

        banned = forbidden_phrases(f"{subject}\n{body}", pack.messaging.avoid_phrases)
        if banned:
            problems.append(f"it uses forbidden phrases {banned}")

        email = Email(to=lead.contact.email, subject=subject, body=body, value_prop=value_prop,
                      lessons_used=[lesson_id for lesson_id in draft.lessons_used if lesson_id in offered])
        return email, problems


def _prompt(lead: Lead, pack: SegmentPack) -> str:
    return (f"Company: {lead.company}\nContact: {lead.contact.name}, {lead.contact.title}\n"
            f"Segment: {pack.display_name}\n\nWrite and send the first email to {lead.contact.name}.")


def lessons_text(lessons: list[Lesson]) -> str:
    return "\n".join(f"- [{lesson.id}] {lesson.text}" for lesson in lessons) or "- (none yet)"


def without_greeting(opening: str) -> str:
    """Safety net for the prompt rule: drop a leading "Hi Maya," and capitalise what's left."""
    text = GREETING.sub("", opening.strip(), count=1)
    return text[:1].upper() + text[1:] if text else opening.strip()


def forbidden_phrases(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if re.search(rf"\b{re.escape(p)}\b", text, re.IGNORECASE)]
