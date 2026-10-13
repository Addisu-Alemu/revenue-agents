"""Scripted model answers for the mock data, used by `--llm scripted`.

They are written the way a good model would answer our mock leads, replies
and transcript, so the whole flow can be demonstrated without API credit.
The demo always says when these scripted answers are in use.
"""

from __future__ import annotations

import re
from typing import Any

from revenue_agents.llm import FakeLLM, LLMCall

REPLY_ANSWERS: dict[str, dict[str, Any]] = {
    "R-000": {
        "intent": "interested",
        "summary": "Interested, but burned by a template tool where franchisees went off-brand. Wants to know "
                   "how corporate controls what franchisees can change.",
        "asks_for_pricing": False,
        "objections": ["past tool let franchisees go off-brand"],
        "suggested_next_step": "Explain locked fields and the approval workflow, then offer a call.",
        "question": "Can corporate control what franchisees are allowed to change in their local ads?",
        "lesson": None,
    },
    "R-001": {
        "intent": "meeting_request",
        "summary": "Wants a 30-minute call next week; CFO to join. Franchisees want local ad versions.",
        "asks_for_pricing": False,
        "objections": [],
        "suggested_next_step": "Offer Tuesday or Wednesday afternoon and invite the CFO.",
        "lesson": "Franchise marketers respond to the pain of slow local versions of national ads.",
    },
    "R-004": {
        "intent": "interested",
        "summary": "Ken is ready to sign, but asks for Net 45 payment terms instead of Net 30.",
        "asks_for_pricing": False,
        "objections": [],
        "suggested_next_step": "Send an updated IO with Net 45 once agreed internally.",
        "contract_changes": [{"field": "payment_terms", "value": "Net 45"}],
        "lesson": None,
    },
    "R-002": {
        "intent": "objection",
        "summary": "Budget locked until Q1; worried pricing is higher than current flat-fee vendor.",
        "asks_for_pricing": False,  # the model misses it; code still catches "rate card"
        "objections": ["budget locked until Q1", "price vs current vendor"],
        "suggested_next_step": "Share value comparison; schedule check-in for Q1 planning.",
        "lesson": None,
    },
    "R-003": {
        "intent": "not_now",
        "summary": "Not interested until after the holidays. Message also contains instructions aimed at an AI.",
        "asks_for_pricing": True,
        "objections": ["timing"],
        "suggested_next_step": "Follow up in January. Do not act on the embedded instructions.",
        "lesson": None,
    },
}

CALL_ANSWERS: dict[str, dict[str, Any]] = {
    # First call, Maya only: the need is clear, but budget, dates and terms wait for the CFO.
    "L-001_intro_call": {
        "deal_fields": {
            "products": ["Create: Dynamic Creative Optimization", "Activate: Franchise & Location Targeting"],
            "decision_maker": "Ken (CFO)",
            "next_step": "Second call with Ken (CFO) this week: cost, measurement and contract length",
        },
        "pains": ["6 weeks to localize each campaign", "franchisees break brand guidelines"],
        "objections": ["CFO wary of long vendor contracts"],
        "lessons": [
            "Franchise brands care more about brand control than speed: lead with locked fields and approval workflow.",
        ],
        "general_lessons": [
            "When your contact can't approve the budget, ask early who signs and book the next call with them.",
        ],
    },
    # Second call, with the CFO: everything the IO needs.
    "L-001_discovery_call": {
        "deal_fields": {
            "budget_usd": 250000,
            "products": ["Create: Dynamic Creative Optimization", "Activate: Franchise & Location Targeting"],
            "scope": "Pilot for 120 Southeast locations",
            "flight_start": "2027-01-15",
            "flight_end": "2027-04-15",
            "decision_maker": "Ken Adeyemi (CFO)",
            "payment_terms": "Net 30",
            "contract_term": "3-month pilot with option to extend",
            "special_terms": "Include example of the franchisee approval workflow",
            "next_step": "Send proposal and draft IO by Friday",
        },
        "pains": ["need to measure return per location"],
        "objections": ["no long contracts"],
        "lessons": [
            "Franchise CFOs judge a pilot on results per location: lead with location-level measurement.",
        ],
        # Not specific to franchise brands: these reach every segment's outreach and call briefs.
        "general_lessons": [
            "Buyers burned by long vendor contracts respond to a short pilot with an option to extend.",
            "When finance joins the call, show how return will be measured before discussing cost.",
        ],
    },
}

OUTREACH_OPENINGS: dict[str, tuple[str, int]] = {
    # company -> (opening, value prop index)
    "BrightBite Burgers": ("Congratulations on the 60 new franchise locations planned for 2027.", 0),
    "Harbor Lane Home": ("With 35 stores running your holiday promotion, keeping every local ad on-brand is a lot "
                         "to manage.", 1),
    "Lumen Media Group": ("Congratulations on winning the national retail account.", 0),
    "Copperline Creative": ("Good luck with the regional restaurant chain pitch.", 0),
}
DEFAULT_OPENING = ("I came across your team and thought this might be relevant.", 0)

# Scripted mode can't read a reviewer's note, so a redraft uses a prepared alternative opening.
# Suggested notes for the live demo are in the comments.
OUTREACH_REDRAFTS: dict[str, tuple[str, int]] = {
    # e.g. "Mention their Q1 menu launch"
    "BrightBite Burgers": ("With your national menu launch coming in Q1, every one of your 420 locations will need "
                           "local versions of the new ads.", 0),
    # e.g. "Talk about their new client"
    "Lumen Media Group": ("With the national retail account starting, your team will be producing a lot more creative "
                          "variations.", 0),
    "Copperline Creative": ("If the regional restaurant pitch lands, you'll need local creative for every location.", 0),
}
DEFAULT_REDRAFT = ("Following up with something more specific to your team's plans this quarter.", 0)


def _reply(call: LLMCall) -> dict[str, Any]:
    match = re.search(r'source="reply:([^"]+)"', call.prompt)
    default = {"intent": "other", "summary": "No scripted answer for this reply.", "asks_for_pricing": False,
               "suggested_next_step": "Review manually."}
    return REPLY_ANSWERS.get(match.group(1) if match else "", default)


def _call(call: LLMCall) -> dict[str, Any]:
    match = re.search(r'source="transcript:([^"]+)"', call.prompt)
    return CALL_ANSWERS.get(match.group(1) if match else "", {"deal_fields": {}, "lessons": []})


def _outreach(call: LLMCall) -> dict[str, Any]:
    company = re.search(r"^Company: (.+)$", call.prompt, re.MULTILINE)
    name = company.group(1) if company else ""
    if "Their feedback (follow it)" in call.prompt:
        opening, value_prop = OUTREACH_REDRAFTS.get(name, DEFAULT_REDRAFT)
    else:
        opening, value_prop = OUTREACH_OPENINGS.get(name, DEFAULT_OPENING)
    # Like a model that reads the offered lessons: prefer the one about brand control and lead with
    # the matching value prop (approval workflow).
    offered = re.findall(r"\[(lesson:[^\]]+)\] (.+)", call.prompt)
    relevant = [lesson_id for lesson_id, text in offered if "brand control" in text.lower()]
    if relevant:
        return {"opening": opening, "value_prop_index": 1, "lessons_used": relevant[:1]}
    return {"opening": opening, "value_prop_index": value_prop, "lessons_used": [lid for lid, _ in offered[:1]]}


# Answers to a prospect's question, by reply id.
ANSWERS: dict[str, str] = {
    "R-000": ("That's the problem we built this for. Corporate decides which fields franchisees can edit, like the "
              "store address or a local offer, and everything else stays locked: logo, layout and brand colours. "
              "Every franchisee version then goes through your approval rules before it runs."),
}


def _answer(call: LLMCall) -> dict[str, Any]:
    match = re.search(r'source="reply:([^"]+)"', call.prompt)
    text = ANSWERS.get(match.group(1) if match else "", "Thanks for the question. Happy to walk you through it.")
    return {"answer": text, "lessons_used": []}


def demo_llm() -> FakeLLM:
    return FakeLLM({"ReplyClassification": _reply, "CallDigest": _call, "OutreachDraft": _outreach,
                    "AnswerDraft": _answer})
