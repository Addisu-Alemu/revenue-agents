"""Words for the desk pages: one line of context for each thing that waits for you, and the rule that stopped it.

Plain functions with no Streamlit, read from what the system recorded (the CRM, approvals and the trace).
"""

from __future__ import annotations

from revenue_agents.guardrails.approvals import ApprovalRequest, ApprovalStatus
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.system import System
from revenue_agents.tracing import Span

RULE_WORDS = {"first_touch_tier": "first contact with a top-tier lead", "reply_to_top_tier": "an answer to a top-tier lead",
              "high_risk_action": "an IO always needs a human", "pricing_in_email": "pricing always needs a human",
              "unscored_lead": "an email to a lead with no score"}


def context_line(system: System, request: ApprovalRequest) -> str:
    """Why this item exists, in one line: what the email is built around, the question it answers, the IO change."""
    action = request.action
    timeline = system.crm.timeline(action.deal_id)
    if action.type is ActionType.SEND_IO:
        changes = action.payload.get("requested_changes")
        if changes:
            asked = ", ".join(f"{name.replace('_', ' ')} {value}" for name, value in changes.items())
            return f"The customer asked for {asked} (in {action.payload['requested_in']}); the IO sent earlier is " \
                   f"voided once this one goes out."
        deal = system.crm.get_deal(action.deal_id)
        return f"Built from the deal's checked fields: {', '.join(deal.fields.products) or 'no products yet'}."
    if ":reply:" in action.id:
        reply_id = action.id.rsplit(":", 1)[-1]
        replies = [a for a in timeline if a.kind == "reply" and a.summary.startswith(f"Reply {reply_id}:")]
        question = replies[-1].data.get("classification", {}).get("question") if replies else None
        return f"Answers their question: {question}" if question else "An answer to their reply."
    if ":v" in action.id.rsplit(":", 1)[-1]:
        earlier = [r for r in system.approvals.all() if r.action.deal_id == action.deal_id
                   and r.status is ApprovalStatus.REJECTED and r.note and action.id.startswith(r.id)]
        return f"Redrafted after your note: “{earlier[-1].note.strip()}”" if earlier else "A redraft after your note."
    notes = [a.data for a in timeline if a.kind == "research"]
    return f"Built around: {notes[-1]['why_now']}" if notes else request.reason


def stopped_by(spans: list[Span], request: ApprovalRequest) -> tuple[str, str]:
    """The policy rule that stopped this action (as the trace recorded it), and the rule in plain words."""
    rule = next((span.data.get("rule", "") for span in spans
                 if span.kind == "policy" and span.data.get("action_id") == request.id), "")
    return rule, RULE_WORDS.get(rule, request.reason)
