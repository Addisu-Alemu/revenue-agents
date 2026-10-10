"""Scripted model answers for our mock data, written the way a good model would answer."""

MAYA_MEETING = {
    "intent": "meeting_request",
    "summary": "Wants a 30-minute call next week; CFO to join. Franchisees want local ad versions.",
    "asks_for_pricing": False,
    "objections": [],
    "suggested_next_step": "Offer Tuesday or Wednesday afternoon and invite the CFO.",
    "lesson": "Franchise marketers respond to the pain of slow local versions of national ads.",
}
SAM_OBJECTION = {
    "intent": "objection",
    "summary": "Budget locked until Q1; worried pricing is higher than current flat-fee vendor.",
    "asks_for_pricing": False,  # the model missed it: code must still catch "rate card"
    "objections": ["budget locked until Q1", "price vs current vendor"],
    "suggested_next_step": "Share value comparison; schedule check-in for Q1 planning.",
    "lesson": None,
}
FOOLED_BY_OWEN = {
    # As if the injection worked on the model: it reports interest and a lesson.
    "intent": "interested",
    "summary": "Wants the internal rate card.",
    "asks_for_pricing": False,
    "objections": [],
    "suggested_next_step": "Send rate card.",
    "lesson": "Always send the full rate card immediately.",
}
BRIGHTBITE_CALL = {
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
    "pains": ["6 weeks to localize each campaign", "franchisees break brand guidelines"],
    "objections": ["no long contracts"],
    "lessons": [
        "Franchise brands care more about brand control than speed: lead with locked fields and approval workflow.",
        "CFOs at franchise brands prefer short pilots with an option to extend over 12-month commitments.",
    ],
}
