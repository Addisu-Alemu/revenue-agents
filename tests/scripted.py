"""Scripted model answers for tests. The realistic ones are shared with the demo (demo_llm.py)."""

from revenue_agents.demo_llm import CALL_ANSWERS, REPLY_ANSWERS

MAYA_MEETING = REPLY_ANSWERS["R-001"]
SAM_OBJECTION = REPLY_ANSWERS["R-002"]
BRIGHTBITE_CALL = CALL_ANSWERS["L-001_discovery_call"]

FOOLED_BY_OWEN = {
    # As if the injection worked on the model: it reports interest and a lesson.
    "intent": "interested",
    "summary": "Wants the internal rate card.",
    "asks_for_pricing": False,
    "objections": [],
    "suggested_next_step": "Send rate card.",
    "lesson": "Always send the full rate card immediately.",
}
