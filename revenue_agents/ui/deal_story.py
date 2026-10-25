"""The Deal room's content, read from the system of record: the story so far, the plan from here, and each deal
field with where it came from.

Plain functions with no Streamlit, so they can be tested on their own. Everything comes from what the system
recorded (the CRM timeline and messages, approvals, the world's timers), never from a script.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Deal, Message, Stage
from revenue_agents.system import System
from revenue_agents.world import World

INTENT_VERBS = {"meeting_request": "asked for a call", "interested": "replied", "objection": "raised a concern",
                "not_now": "said not now", "unsubscribe": "unsubscribed", "other": "replied"}
LESSON_AUTHORS = {"call": "Call intel", "reply": "Reply agent", "review": "You"}
GREETING = re.compile(r"^(hi|hello|hey|dear)\b", re.IGNORECASE)
FIELD_LABELS = {"budget_usd": "Budget", "products": "Products", "scope": "Scope", "flight": "Flight",
                "decision_maker": "Signs", "payment_terms": "Payment", "contract_term": "Term",
                "special_terms": "Special terms", "next_step": "Next step"}
QUIET = ("stage_change", "flag")  # shown elsewhere: the stage bar and the flags


@dataclass
class Beat:
    """One moment in the deal's story."""

    at: dt.datetime
    actor: str  # "Research agent", "Maya", "You", ...
    text: str
    quote: str | None = None
    tone: str = "agent"  # agent, customer, you, system


@dataclass
class FieldRow:
    label: str
    value: str
    source: str  # who said it and when, or how it was set
    quote: str | None = None


@dataclass
class Plan:
    steps: list[tuple[bool, str]] = field(default_factory=list)  # (needs you, what happens)


def story(system: System, deal: Deal) -> list[Beat]:
    """What happened on this deal, in order, in words."""
    lead = system.crm.get_lead(deal.lead_id)
    first = lead.contact.name.split()[0]
    messages = {message.id: message for message in system.crm.messages(deal.id)}
    inbound = [message for message in messages.values() if message.direction == "inbound"]
    lessons = {lesson.id: lesson for lesson in system.lessons.all()}
    approvals = {request.id: request for request in system.approvals.all()}
    beats = [Beat(lead.created_at, lead.contact.name, f"came in as a new lead ({lead.source.replace('_', ' ')}).",
                  tone="customer")]

    for activity in system.crm.timeline(deal.id):
        kind, data, at = activity.kind, activity.data, activity.at
        if kind in QUIET or activity.summary.startswith(("Enrich ", "Score ")):
            continue
        if kind == "research":
            beats.append(Beat(at, "Research agent", _research(deal) + f" Why now: {data['why_now']}"))
        elif kind == "email_sent":
            message = messages.get(data.get("message_id", ""))
            beats.append(_email(message, approvals, data))
        elif kind == "reply":
            message = next((m for m in inbound if abs((m.at - at).total_seconds()) < 1), None)
            beats.append(_reply(first, data, message))
        elif kind == "note" and activity.summary.startswith("Lesson: "):
            lesson = lessons.get(data.get("lesson_id", ""))
            author = LESSON_AUTHORS.get(lesson.source, "An agent") if lesson else "An agent"
            scope = " (for every segment)" if lesson and lesson.scope == "global" else ""
            beats.append(Beat(at, author, f"learned a lesson{scope}: {activity.summary[8:]}",
                              tone="you" if author == "You" else "agent"))
        elif kind == "note" and activity.summary.startswith("Deal fields from call"):
            filled = ", ".join(dict.fromkeys(_label(name) for name in data.get("fields", []) if name != "next_step"))
            beats.append(Beat(at, "Call intel", f"read the call and filled in: {filled or 'nothing new'}."))
        elif kind == "note" and activity.summary.startswith("Stays in "):
            stage, missing = activity.summary.split(": ", 1)
            needed = ", ".join(dict.fromkeys(_label(name) for name in missing.split(" needs ", 1)[-1].split(", ")))
            beats.append(Beat(at, "Orchestrator", f"kept the deal at {stage.removeprefix('Stays in ')}: a proposal "
                                                  f"still needs {needed}.", tone="system"))
        elif kind == "meeting_booked":
            beats.append(Beat(at, "Reply agent", f"booked the call on its own: {activity.summary}."))
        elif kind == "call_brief":
            text = activity.summary.replace("Call brief ready for the rep: ", "prepared a brief for the rep: ")
            text = text.replace("Call brief updated", "updated the brief").replace("budget usd", "budget")
            beats.append(Beat(at, "Call brief", text + "."))
        elif kind == "call":
            beats.append(Beat(at, "Fireflies", f"sent the transcript of {data['transcript_id']}.", tone="system"))
        elif kind == "document":
            beats.append(Beat(at, "Deal desk", f"drafted {data['io_number']} from the deal's checked fields."))
        elif kind == "io_sent":
            request = approvals.get(f"send_io:{deal.id}:{data['io_number']}")
            who = f" after you approved it ({request.decided_by})" if request and request.decided_by else ""
            beats.append(Beat(at, "Deal desk", f"sent {data['io_number']} for e-signature to {data['to']}{who}."))
        elif kind == "io_voided":
            beats.append(Beat(at, "Deal desk", activity.summary + ", so only the newest IO can be signed."))
        elif kind == "signed":
            io_number = activity.summary.split(" signed by ")[0]
            beats.append(Beat(at, data.get("signed_by", "The customer"), f"signed {io_number}.", tone="customer"))
        elif kind == "handoff":
            beats.append(Beat(at, "Orchestrator", "verified the signature with the e-sign provider: closed won, "
                                                  "handed off to delivery.", tone="system"))
        else:
            beats.append(Beat(at, "System", activity.summary, tone="system"))
    return sorted(beats, key=lambda beat: beat.at)


def plan(system: System, world: World | None, deal: Deal) -> Plan:
    """What happens next on this deal: what waits for you first, then what the system will do."""
    result = Plan()
    now = world.now() if world and world.exists() else dt.datetime.now(dt.timezone.utc)
    for request in system.approvals.pending():
        if request.action.deal_id == deal.id:
            waited = _ago(now - request.created_at)
            result.steps.append((True, f"You decide: {request.action.summary}. Waiting {waited}."))
    for activity in system.crm.timeline(deal.id):
        if activity.kind == "meeting_booked" and dt.datetime.fromisoformat(activity.data["when"]) > now:
            result.steps.append((False, f"Call on {dt.datetime.fromisoformat(activity.data['when']):%a %d %b %H:%M}; "
                                        "then call intel fills in the deal fields from the transcript."))
    if world and world.exists():
        for item in world.upcoming(("timer",)):
            if (item.event or {}).get("payload", {}).get("deal_id") == deal.id:
                result.steps.append((False, f"{item.at:%a %d %b}: {item.label} (a timer the reply agent set)."))
    result.steps += [(False, text) for text in _next_by_stage(system, deal, bool(result.steps))]
    return result


def fields(system: System, deal: Deal) -> list[FieldRow]:
    """Each known deal field, with the words it came from (checked against the transcript) or how it was set."""
    origin: dict[str, tuple[str, str | None]] = {}
    for activity in system.crm.timeline(deal.id):
        if activity.kind != "note":
            continue
        for name in activity.data.get("fields", []):
            source = activity.data.get("sources", {}).get(name)
            if source:
                speaker = source["speaker"].split()[0]
                origin[name] = (f"{speaker}, {_call_name(source['transcript_id'])} {source['at']}", source["quote"])
            else:
                origin[name] = (activity.summary, None)

    values = deal.fields
    rows = []
    for name in ("budget_usd", "products", "scope", "flight", "decision_maker", "payment_terms", "contract_term",
                 "special_terms", "next_step"):
        if name == "flight":
            if not (values.flight_start and values.flight_end):
                continue
            value = f"{values.flight_start:%d %b} to {values.flight_end:%d %b %Y}"
            source, quote = origin.get("flight_start", ("", None))
        else:
            raw = getattr(values, name)
            if raw in (None, "", []):
                continue
            value = f"${raw:,}" if name == "budget_usd" else (", ".join(raw) if isinstance(raw, list) else str(raw))
            source, quote = origin.get(name, ("", None))
        rows.append(FieldRow(FIELD_LABELS[name], value, source or "set before quotes were recorded", quote))
    return rows


def champion_and_signer(system: System, deal: Deal) -> tuple[str, str | None]:
    lead = system.crm.get_lead(deal.lead_id)
    return f"{lead.contact.name}, {lead.contact.title}", deal.fields.decision_maker


# --- Helpers ---------------------------------------------------------------------------


def _research(deal: Deal) -> str:
    parts = []
    if deal.enrichment:
        e = deal.enrichment
        signals = ", ".join(signal.type for signal in e.signals) or "no buying signals"
        parts.append(f"found {e.locations} locations, ${e.annual_ad_spend_usd:,} a year on ads, {signals}.")
    if deal.score:
        parts.append(f"Score {deal.score.total:g}, Tier {deal.score.tier.value}.")
    return " ".join(parts) or "looked the company up."


def _email(message: Message | None, approvals: dict, data: dict) -> Beat:
    answer = data.get("subject", "").lower().startswith("re:")
    actor = "Reply agent" if answer else "Outreach agent"
    what = "answered by email" if answer else f"sent the first email: “{data.get('subject', '')}”"
    request = approvals.get(message.meta.get("action_id", "")) if message else None
    if request and request.status is ApprovalStatus.APPROVED and request.action.type is ActionType.SEND_EMAIL:
        edited = " with your edits" if request.original_action else ""
        how = f"; it waited for you and you approved it{edited}"
    else:
        how = "; low risk, so it went out on its own"
    lessons = " It used a lesson the team learned." if data.get("lessons_used") else ""
    return Beat(message.at if message else dt.datetime.now(dt.timezone.utc), actor, f"{what}{how}.{lessons}")


def _reply(first: str, data: dict, message: Message | None) -> Beat:
    intent = data.get("classification", {}).get("intent", "other")
    flags = data.get("flags", [])
    asked = data.get("classification", {}).get("question")
    text = ("asked a question first." if asked and intent in ("interested", "objection")
            else INTENT_VERBS.get(intent, "replied") + ".")
    if "possible_prompt_injection" in flags:
        text += " The email held instructions aimed at an AI: ignored and flagged."
    if "pricing_requested" in flags:
        text += " It asks about price, so a human answers."
    at = message.at if message else dt.datetime.now(dt.timezone.utc)
    return Beat(at, first, text, quote=_key_line(message.body) if message else None, tone="customer")


def _key_line(body: str) -> str:
    """The first sentence that says something (not a greeting, thanks or sign-off), cut to a readable length."""
    for line in body.splitlines():
        line = line.strip()
        if not line or GREETING.match(line) or line.lower().startswith(("best", "regards", "cheers")):
            continue
        for sentence in re.split(r"(?<=[.?!])\s+", line):
            if sentence and not sentence.lower().startswith(("thanks", "thank you")):
                return sentence if len(sentence) <= 180 else sentence[:177].rstrip() + "…"
    return body.strip()[:180]


def _next_by_stage(system: System, deal: Deal, has_steps: bool) -> list[str]:
    pack = system.segments.get(deal.segment)
    limit = pack.tracking.limit_for(deal.stage)
    stall = f" The morning SLA check flags it after {limit} days without progress." if limit else ""
    signer = deal.fields.decision_maker or "the signer"
    stage = deal.stage
    if stage is Stage.SCORED and deal.score and deal.score.tier not in pack.messaging.outreach_tiers:
        return [f"Tier {deal.score.tier.value}: no outreach. It stays in nurture."]
    if stage in (Stage.PROSPECT, Stage.ENRICHED, Stage.SCORED):
        return [] if has_steps else ["The first email goes out once it is drafted and allowed."]
    if stage is Stage.OUTREACH:
        return [f"Waiting for a reply; the reply agent reads it as soon as it arrives.{stall}"]
    if stage is Stage.REPLIED:
        return [f"The reply agent answers questions and books a call when they ask for one.{stall}"]
    if stage is Stage.MEETING:
        return [] if has_steps else [f"After the call, call intel fills in the deal fields.{stall}"]
    if stage in (Stage.QUALIFIED, Stage.PROPOSAL):
        return [f"Once you approve the IO, the e-sign envelope goes to {signer}."]
    if stage is Stage.SIGNATURE:
        return [f"{signer} signs; the signature is checked with the e-sign provider, then closed won and handed to "
                f"delivery. A forged webhook is rejected.{stall}"]
    if stage is Stage.HANDOFF:
        return ["Closed won and handed off to delivery. Nothing left to do here."]
    return ["Closed lost. Nothing left to do here."]


def _label(name: str) -> str:
    return FIELD_LABELS["flight" if name.startswith("flight") else name].lower() if name in FIELD_LABELS or \
        name.startswith("flight") else name.replace("_", " ")


def _call_name(transcript_id: str) -> str:
    return "intro call" if "intro" in transcript_id else ("discovery call" if "discovery" in transcript_id else "call")


def _ago(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} days"
