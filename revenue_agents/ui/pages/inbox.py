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
from revenue_agents.team import LABELS
from revenue_agents.ui import components, context, desk_views, feed, live


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
    spans = system.tracer.spans()
    now = desk.world.now()
    left, middle, right = st.columns([1.6, 3.6, 1.7], gap="medium")
    with left:
        for request in pending:
            deal = system.crm.get_deal(request.action.deal_id)
            lead = system.crm.get_lead(deal.lead_id)
            tier = f"Tier {deal.score.tier.value} · {deal.score.total:g}" if deal.score else "no tier"
            _, rule = desk_views.stopped_by(spans, request)
            if st.button(f"{lead.company} · {tier}  \n**{request.action.summary}**  \nWaiting "
                         f"{_ago(now - request.created_at)} · {rule}", key=f"pick-{request.id}",
                         type="primary" if request.id == selected else "secondary", width="stretch"):
                st.session_state.inbox_selected = request.id
                st.rerun()
        st.caption("Low-risk work never lands here: enrichment, scoring, lower-tier emails, meeting booking, CRM "
                   "updates and lessons run on their own and show up on Today.")
        for index, request in enumerate(waiting):
            _waiting_card(desk, request, f"waiting-{index}")
    if selected:
        request = system.approvals.get(selected)
        run = feed.trail_for(spans, request.id)
        with middle:
            _detail(desk, system, spans, request, run)
        with right:
            _trail(run)
            if request.action.type is ActionType.SEND_EMAIL:
                _evidence(system, system.crm.get_deal(request.action.deal_id), request.action.payload)
    _history(system)


def _detail(desk, system, spans, request, run) -> None:
    action = request.action
    deal = system.crm.get_deal(action.deal_id)
    lead = system.crm.get_lead(deal.lead_id)
    key = f"detail-{request.id}"
    rule, words = desk_views.stopped_by(spans, request)
    asked = next((step.tool for step in run.tools if step.tone == "asked"), None) if run else None
    where = f"PreToolUse hook on {asked}, then the gateway" if asked else "the gateway (the deal desk is plain code)"
    tier = f"Tier {deal.score.tier.value} · {deal.score.total:g}" if deal.score else "no tier"
    with st.container(border=True):
        st.markdown(f"<div class='ra-meta'>{_html(lead.company)} · {tier} · {deal.segment} pack · proposed by "
                    f"{LABELS.get(action.agent, action.agent)} at {request.created_at:%a %H:%M}</div>"
                    f"<h3 class='ra-detail-title'>{_html(action.summary)}</h3>"
                    f"<div class='ra-stopped'><b>● Why it stopped: {_html(words)}</b><div>{_html(request.reason)}. "
                    f"The rule is code; no agent or prompt can change it.</div>"
                    f"<code>policy.{rule or 'rule'} · {where}</code></div>", unsafe_allow_html=True)

    edits: dict[str, str] = {}
    is_email = action.type is ActionType.SEND_EMAIL
    with st.container(border=True):
        if is_email:
            version = action.id.rsplit(":", 1)[-1] if ":v" in action.id else "v1"
            st.markdown(f"<div class='ra-tag'>{version} · agent draft</div>", unsafe_allow_html=True)
            edits = components.editable_email(action.payload, key)
        elif action.type is ActionType.SEND_IO:
            requested = action.payload.get("requested_changes")
            if requested:
                asked_for = "; ".join(f"{name.replace('_', ' ')} {getattr(deal.fields, name)} → {value}"
                                      for name, value in requested.items())
                st.info(f"**The customer asked for this change** (in {action.payload['requested_in']}): {asked_for}. "
                        f"Approve to send this version: the deal takes the new terms and the IO sent earlier is "
                        f"voided, so only this one can be signed. Reject to keep the current IO.",
                        icon=":material/mark_email_read:")
            components.io_preview(action.payload["document"])
            with st.expander("Change the terms (the deal desk redrafts the IO)", icon=":material/edit_document:"):
                components.terms_form(desk, deal, f"{key}-terms")

    with st.container(key="ra-coach-inbox" if is_email else "ra-decide"):
        if is_email:
            st.markdown("**Coach the agent**")
            st.caption("Say what to change in plain words. The agent redrafts; the policy still decides what may be "
                       "sent.")
            note = st.text_area("Your note", key=f"{key}-note", height=70, label_visibility="collapsed",
                                placeholder="e.g. Mention their Q1 menu launch")
            keep = st.checkbox(f"Keep my note as a lesson for {deal.segment} first emails", key=f"{key}-lesson",
                               help="Only for advice that fits other leads too, not a one-off fix.")
        else:
            note = st.text_input("Note (optional)", key=f"{key}-note", placeholder="e.g. checked the terms with legal")
            keep = False
        buttons = st.columns([1.6, 1.6, 1.6])
        if is_email and buttons[0].button("Redraft", key=f"{key}-coach", icon=":material/edit_note:",
                                          disabled=not note.strip(), width="stretch",
                                          help="Reject with your note: the agent writes the next version following it"):
            _decide(desk, system, request, False, note, keep)
        if buttons[1].button("Reject", key=f"{key}-reject", width="stretch"):
            _decide(desk, system, request, False, note if not is_email else "", False)
        if buttons[2].button("Approve with edits" if edits else "Approve", key=f"{key}-approve", type="primary",
                             icon=":material/check:", width="stretch"):
            if any(not value.strip() for value in edits.values()):
                st.error("The subject and the email can't be empty.", icon=":material/error:")
            else:
                _decide(desk, system, request, True, note, False, edits)


def _trail(run) -> None:
    """What the agent did before it stopped: each tool it chose, in order, how long it took and what it got back."""
    if run is None:
        return
    rows = "".join(
        f"<div class='ra-tool'><div><code>{_html(step.tool)}</code><span>{step.ms / 1000:.1f} s</span></div>"
        f"<p>{_html(_first_line(step.result))}</p></div>" for step in run.tools)
    with st.container(border=True):
        st.markdown(f"<h5>What the agent did</h5><div class='ra-muted'>{_html(run.label)} · {_html(run.model)}</div>"
                    f"{rows}", unsafe_allow_html=True)


def _first_line(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= 110 else line[:107].rstrip() + "…"


def _ago(delta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    return f"{minutes // 60} h" if minutes < 48 * 60 else f"{minutes // 1440} days"


def _html(text: str) -> str:
    """Text shown inside HTML: no markup, dollar signs as text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace("$", "&#36;")


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
