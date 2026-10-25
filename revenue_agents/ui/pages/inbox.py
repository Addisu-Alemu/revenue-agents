"""Inbox: what the rules sent to you, why it stopped, what the agent did to get there, and your decision.

Approve sends it (your edits, if you made any). Reject stops it. Coach rejects
with a note: the agent writes the next version following it, and the note can
become a lesson for every later email. Your decision goes to the desk and is
done in a moment, even while the clock is paused.
"""

from __future__ import annotations

import streamlit as st

from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.ui import agent_views, components, context, feed, live


def render() -> None:
    desk = context.get_desk()
    system = desk.system()
    components.page_header("Inbox", "Only what the rules send to you: first contact with top tiers, answers to top "
                                    "tiers, anything about price, and every IO. The rules are code, so no agent or "
                                    "prompt can skip them.")
    pending = sorted((r for r in system.approvals.pending()
                      if context.in_segment(system.crm.get_deal(r.action.deal_id))), key=lambda r: r.created_at)
    waiting = system.orchestrator.approved_waiting()
    if not pending and not waiting:
        st.success("Nothing waits for you. New items appear here the moment an agent stops for you.",
                   icon=":material/task_alt:")
        _history(system)
        return

    ids = [request.id for request in pending]
    selected = st.session_state.get("inbox_selected")
    if selected not in ids:
        selected = ids[0] if ids else None
    left, right = st.columns([2, 5], gap="large")
    with left:
        st.markdown(f"##### Waiting · {len(pending)}")
        for request in pending:
            deal = system.crm.get_deal(request.action.deal_id)
            lead = system.crm.get_lead(deal.lead_id)
            tier = f"Tier {deal.score.tier.value}" if deal.score else "no tier"
            if st.button(f"**{lead.company}** · {tier}  \n{request.action.summary}", key=f"pick-{request.id}",
                         type="primary" if request.id == selected else "secondary", width="stretch"):
                st.session_state.inbox_selected = request.id
                st.rerun()
        for index, request in enumerate(waiting):
            _waiting_card(desk, request, f"waiting-{index}")
    with right:
        if selected:
            _detail(desk, system, system.approvals.get(selected))
    _history(system)


def _detail(desk, system, request) -> None:
    action = request.action
    deal = system.crm.get_deal(action.deal_id)
    lead = system.crm.get_lead(deal.lead_id)
    key = f"detail-{request.id}"
    with st.container(border=True):
        top, badge = st.columns([5, 1.5])
        icon = ":material/mail:" if action.type is ActionType.SEND_EMAIL else ":material/description:"
        top.markdown(f"#### {icon} {action.summary}")
        top.markdown(f"<span class='ra-muted'>{lead.contact.name}, {lead.contact.title} · {lead.company} · {deal.id} · "
                     f"proposed by the {action.agent} agent</span>", unsafe_allow_html=True)
        with badge:
            components.tier_badge(deal)
        st.warning(f"**Why it stopped:** {request.reason}. The rule lives in `guardrails/policy.py`; the agent "
                   f"can't change it.", icon=":material/policy:")

        edits: dict[str, str] = {}
        if action.type is ActionType.SEND_EMAIL:
            edits = components.editable_email(action.payload, key)
        elif action.type is ActionType.SEND_IO:
            requested = action.payload.get("requested_changes")
            if requested:
                asked = "; ".join(f"{name.replace('_', ' ')} {getattr(deal.fields, name)} → {value}"
                                  for name, value in requested.items())
                st.info(f"**The customer asked for this change** (in {action.payload['requested_in']}): {asked}. "
                        f"Approve to send this version: the deal takes the new terms and the IO sent earlier is "
                        f"voided, so only this one can be signed. Reject to keep the current IO.",
                        icon=":material/mark_email_read:")
            components.io_preview(action.payload["document"])
            with st.expander("Change the terms (the deal desk redrafts the IO)", icon=":material/edit_document:"):
                components.terms_form(desk, deal, f"{key}-terms")

        is_email = action.type is ActionType.SEND_EMAIL
        if is_email:
            st.markdown("**Coach the agent**")
            note = st.text_area("Your note", key=f"{key}-note", height=70, label_visibility="collapsed",
                                placeholder="e.g. Mention their Q1 menu launch")
            keep = st.checkbox("Keep as a lesson for every later email", key=f"{key}-lesson",
                               help="Only for advice that fits other leads too, not a one-off fix.")
        else:
            note = st.text_input("Note (optional)", key=f"{key}-note", placeholder="e.g. checked the terms with legal")
            keep = False
        buttons = st.columns([2, 1.6, 1.6, 1.6])
        if is_email and buttons[1].button("Redraft", key=f"{key}-coach", icon=":material/edit_note:",
                                          disabled=not note.strip(), width="stretch",
                                          help="Reject with your note: the agent writes the next version following it"):
            _decide(desk, system, request, False, note, keep)
        if buttons[2].button("Reject", key=f"{key}-reject", width="stretch"):
            _decide(desk, system, request, False, note if not is_email else "", False)
        if buttons[3].button("Approve with edits" if edits else "Approve", key=f"{key}-approve", type="primary",
                             icon=":material/check:", width="stretch"):
            if any(not value.strip() for value in edits.values()):
                st.error("The subject and the email can't be empty.", icon=":material/error:")
            else:
                _decide(desk, system, request, True, note, False, edits)

    _trail(system, request)
    if is_email:
        _evidence(system, deal, action.payload)


def _trail(system, request) -> None:
    run = feed.trail_for(system.tracer.spans(), request.id)
    if run is None:
        return
    with st.container(border=True):
        st.markdown(f"**What the {run.label.lower()} did** · {run.title}")
        st.markdown(agent_views.chips(run.tools))
        with st.expander("Each tool call, with the rule that decided it"):
            agent_views.run_details(run)


def _evidence(system, deal, email: dict) -> None:
    notes = [a.data for a in system.crm.timeline(deal.id) if a.kind == "research"]
    lessons = {lesson.id: lesson for lesson in system.lessons.all()}
    used = [lessons[lesson_id] for lesson_id in email.get("lessons_used", []) if lesson_id in lessons]
    if not (notes or used or deal.score):
        return
    with st.container(border=True):
        st.markdown("**What it was built from**")
        if notes:
            st.markdown(f":material/travel_explore: **Why now** (research agent): {_safe(notes[-1]['why_now'])}")
        for lesson in used:
            st.markdown(f":material/psychology: **Lesson** from {lesson.source} {lesson.source_id}: "
                        f"{_safe(lesson.text)}")
        if deal.score:
            st.markdown(f":material/scoreboard: **Score** {deal.score.total:g} (Tier {deal.score.tier.value}): "
                        f"{_safe(deal.score.fit.reason)}")


def _safe(text: str) -> str:
    """Streamlit reads $...$ as maths: show dollar amounts as text."""
    return text.replace("$", "\\$")


def _decide(desk, system, request, approved: bool, note: str, keep: bool, edits: dict | None = None) -> None:
    with st.spinner("The desk is on it…"):
        done = live.decide(desk.world, system, request, approved, context.reviewer(), note.strip() or None, keep,
                           edits)
    if not done:
        st.toast("Sent to the desk: it will be done as soon as the current step finishes.", icon=":material/schedule:")
    st.session_state.pop("inbox_selected", None)
    st.rerun()


def _waiting_card(desk, request, key: str) -> None:
    """Approved by a human but not executed (agents paused, or a failure). Run now executes it once."""
    system = desk.system()
    with st.container(border=True):
        st.markdown(f":material/pending_actions: **{request.action.summary}**")
        st.caption(f"Approved by {request.decided_by} · not executed yet"
                   + (" · agents are paused" if system.kill_switch.is_engaged() else ""))
        if st.button("Run now", key=f"{key}-run", icon=":material/play_arrow:",
                     disabled=system.kill_switch.is_engaged()):
            live.run_now(desk.world, request)
            st.rerun()


def _history(system) -> None:
    decided = [r for r in system.approvals.all() if r.status is not ApprovalStatus.PENDING
               and context.in_segment(system.crm.get_deal(r.action.deal_id))]
    if not decided:
        return
    with st.expander(f"Your decisions · {len(decided)}", icon=":material/history:"):
        for request in decided:
            approved = request.status is ApprovalStatus.APPROVED
            icon = ":material/check_circle:" if approved else ":material/cancel:"
            note = f" · “{request.note}”" if request.note else ""
            st.markdown(f"{icon} **{request.action.summary}**  \n<span class='ra-muted'>{request.status.value} by "
                        f"{request.decided_by} · {request.decided_at:%a %d %b, %H:%M}{note}</span>",
                        unsafe_allow_html=True)
