"""Approvals: the human-in-the-loop queue for high-risk actions, plus the decision history."""

from __future__ import annotations

import streamlit as st

from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.ui import components, context


def render() -> None:
    system = context.get_system()
    components.page_header("Approvals", "High-risk actions wait here: first contact with top-tier leads, anything "
                                        "with pricing, and IOs. The policy is code, so no agent or prompt can skip it.")
    pending = system.approvals.pending()
    if pending:
        for index, request in enumerate(pending):
            components.approval_card(system, request, key=f"queue-{index}")
    else:
        st.success("Nothing waiting. High-risk actions will appear here.", icon=":material/task_alt:")

    waiting = system.orchestrator.approved_waiting()
    if waiting:
        st.subheader("Approved, not executed yet")
        st.caption("A human approved these, but they didn't run (kill switch on, or a failure). Run each once.")
        for index, request in enumerate(waiting):
            components.waiting_card(system, request, key=f"waiting-{index}")

    decided = [r for r in system.approvals.all() if r.status is not ApprovalStatus.PENDING]
    if decided:
        st.subheader("Decision history")
        for request in decided:
            approved = request.status is ApprovalStatus.APPROVED
            icon = ":material/check_circle:" if approved else ":material/cancel:"
            note = f" · “{request.note}”" if request.note else ""
            st.markdown(f"{icon} **{request.action.summary}**  \n<span class='ra-muted'>{request.status.value} by "
                        f"{request.decided_by} · {request.decided_at:%b %d, %H:%M}{note}</span>",
                        unsafe_allow_html=True)
