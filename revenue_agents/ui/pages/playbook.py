"""Playbook: what the team has learned from calls, replies and your coaching, and whether it is working.

Agents search these lessons before every draft and call brief, and every draft records which lessons it used, so
each card shows where a lesson came from (the words, checked against the source) and everywhere it was used.
"""

from __future__ import annotations

import streamlit as st

from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.ui import components, context, learning, live


def render() -> None:
    desk = context.get_desk()
    system = desk.system()
    components.page_header("Playbook", "What the team has learned from calls, replies and your coaching. Agents "
                                       "search it before every draft and every call brief, and every draft says "
                                       "which lessons it used.")
    left, right = st.columns([3, 2], gap="large")
    with left:
        _lessons(system)
    with right:
        _coaching(desk, system)
        _effect(system)
        _packs(system)


def _lessons(system) -> None:
    st.subheader("Lessons")
    cards = learning.lessons(system, st.session_state.segment)
    if not cards:
        st.caption("Nothing learned yet. Lessons come from call transcripts, replies and your coaching.")
        return
    for card in cards:
        lesson = card.lesson
        quote = f": “{_safe(lesson.quote)}”" if lesson.quote else ""
        scope = ":gray-badge[every segment]" if lesson.scope == "global" else f":gray-badge[{lesson.segment}]"
        used = (f":violet-badge[used in {_badge_text(', '.join(card.used_in))}]" if card.used_in
                else ":gray-badge[not used yet]")
        with st.container(border=True):
            st.markdown(f"**{_safe(lesson.text)}**")
            st.markdown(f"<span class='ra-muted'>{_safe(card.origin)}{quote}</span>", unsafe_allow_html=True)
            st.markdown(f"{scope} {used} <span class='ra-muted'>&nbsp;learned {lesson.created_at:%a %d %b %H:%M}"
                        f"</span>", unsafe_allow_html=True)


def _coaching(desk, system) -> None:
    waiting = [request for request in system.orchestrator.open_coaching()
               if context.in_segment(system.crm.get_deal(request.action.deal_id))]
    if not waiting:
        return
    paused = system.kill_switch.is_engaged()
    for request in waiting:
        deal = system.crm.get_deal(request.action.deal_id)
        company = system.crm.get_lead(deal.lead_id).company
        redrafted = [r for r in system.approvals.all() if r.id.startswith(f"{request.id}:v")
                     and r.status is ApprovalStatus.APPROVED]
        with st.container(key=f"ra-coach-{request.id.replace(':', '-')}"):
            st.markdown("##### Your coaching, not yet a lesson")
            st.markdown(f"**“{_safe(request.note.strip())}”**")
            st.markdown(f"<span class='ra-muted'>Your note on {_safe(company)}'s first email, "
                        f"{request.decided_at:%a %d %b %H:%M}."
                        + (" The redraft you approved followed it." if redrafted else "") + "</span>",
                        unsafe_allow_html=True)
            keep, once = st.columns(2)
            if keep.button(f"Keep for {deal.segment} first emails", key=f"keep-{request.id}", type="primary",
                           width="stretch", disabled=paused):
                live.coaching(desk.world, request, True, context.reviewer())
                st.rerun()
            if once.button("Only for that email", key=f"once-{request.id}", width="stretch", disabled=paused):
                live.coaching(desk.world, request, False, context.reviewer())
                st.rerun()
            if paused:
                st.caption("The agents are paused: a new lesson waits until you release them.")


def _effect(system) -> None:
    result = learning.effect(system)
    (with_sent, with_replied), (without_sent, without_replied) = result.with_lesson, result.without
    with st.container(border=True):
        st.markdown("##### Is it working?")
        st.markdown(f"First emails that used a lesson: **{with_sent}**, replied **{with_replied}**  \n"
                    f"First emails without one: **{without_sent}**, replied **{without_replied}**")
        if result.enough:
            st.caption("Compare the reply rates; a lesson that never helps can be retired.")
        else:
            st.caption(f"Too few emails to judge yet (we'd want {learning.ENOUGH_TO_JUDGE} on each side). Every "
                       "email records the lessons it used and the reply it got, so this fills in as volume grows.")


def _packs(system) -> None:
    with st.container(border=True):
        st.markdown("##### Segment packs")
        st.caption("Everything segment-specific is one YAML file. The agents read it; none of them has segment code.")
        for pack in learning.packs(system):
            with st.container(border=True):
                st.markdown(f"**{pack.title}** <span class='ra-muted'>· {pack.file}</span>", unsafe_allow_html=True)
                st.markdown("<span class='ra-muted'>" + " · ".join(_safe(line) for line in pack.lines) + "</span>",
                            unsafe_allow_html=True)


def _safe(text: str) -> str:
    """Text written by people or the model, shown with HTML on: no markup, dollar signs as text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace("$", "&#36;")


def _badge_text(text: str) -> str:
    """Badges end at "]": keep brackets out."""
    return text.replace("[", "(").replace("]", ")")
