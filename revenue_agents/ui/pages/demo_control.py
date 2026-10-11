"""Live demo: run the story one step at a time, decide approvals inline, watch what happens."""

from __future__ import annotations

import streamlit as st

from revenue_agents import story
from revenue_agents.llm import LLMError
from revenue_agents.ui import agent_views, components, context


def render() -> None:
    if st.session_state.pop("just_reset", False):
        st.toast("Demo reset: deals, approvals, lessons and the audit log were cleared.", icon=":material/check_circle:")
    system = context.get_system()
    components.page_header("Live demo", "Events arrive one at a time. Every step runs through the real "
                                        "orchestrator, policy and gateway.")
    _metrics(system)

    steps = context.steps(system)
    done = [context.is_done(system, step) for step in steps]
    pending = system.approvals.pending()
    upcoming = context.next_step(system)

    st.progress(sum(done) / len(steps), text=f"Story progress: {sum(done)} of {len(steps)} steps")
    with st.container(horizontal=True, vertical_alignment="center"):
        label = f"Next: {upcoming.title}" if upcoming else "Story complete"
        if st.button(label, type="primary", icon=":material/play_arrow:", disabled=upcoming is None or bool(pending)):
            _run(system, upcoming)
        if st.button("Reset demo", icon=":material/restart_alt:"):
            st.session_state.confirm_reset = True
    if st.session_state.get("confirm_reset"):
        _confirm_reset()
    if pending:
        st.info(f"{len(pending)} high-risk action(s) waiting for a human. Decide below to continue.",
                icon=":material/front_hand:")
    if system.kill_switch.is_engaged():
        held = len(system.orchestrator.held_events())
        st.error(f"Kill switch is ON: new events are held ({held} so far), nothing executes. "
                 "Release it on the Guardrails page or with scripts/kill_switch.py off.", icon=":material/pause_circle:")

    left, right = st.columns([3, 2], gap="large")
    with left:
        for index, request in enumerate(pending):
            components.approval_card(system, request, key=f"demo-{index}")
        for index, request in enumerate(system.orchestrator.approved_waiting()):
            components.waiting_card(system, request, key=f"demo-waiting-{index}")
        st.subheader("What just happened")
        if not st.session_state.log:
            st.caption("Nothing yet. Press the button above to send the first event.")
        components.log_entries(system, limit=6)
    with right:
        newest = st.session_state.log[0] if st.session_state.log else None
        spans = system.tracer.spans()[newest.trace[0]:newest.trace[1]] if newest and newest.trace else []
        st.subheader("Who worked on the last step")
        st.graphviz_chart(agent_views.agent_map(spans, len(pending)), width="stretch")
        st.caption("Lit up: agents, policy and stores that worked on the last step. Grey: idle.")
        st.subheader("The story")
        for step, is_done in zip(steps, done):
            if is_done:
                st.markdown(f":material/check_circle: :green[{step.title}]")
            elif step is upcoming:
                st.markdown(f":material/play_circle: **{step.title}**")
            else:
                st.markdown(f":material/radio_button_unchecked: :gray[{step.title}]")


def _close_reset_dialog() -> None:
    st.session_state.confirm_reset = False


@st.dialog("Reset the demo?", icon=":material/restart_alt:", on_dismiss=_close_reset_dialog)
def _confirm_reset() -> None:
    """A modal that closes once you choose (a popover would stay open after the reset)."""
    st.write("This deletes all demo state (deals, approvals, lessons, audit log) so the story starts again. "
             "The kill switch keeps its current setting.")
    cancel, confirm = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        _close_reset_dialog()
        st.rerun()
    if confirm.button("Yes, reset", type="primary", width="stretch"):
        context.reset()
        _close_reset_dialog()
        st.session_state.just_reset = True
        st.rerun()


def _run(system, step: story.Step) -> None:
    start = system.tracer.count()
    with st.status(f"Running: {step.title}", expanded=True) as status:
        try:
            with system.tracer.listening(agent_views.LiveProgress()):  # each agent appears as it works
                lines = context.run_step(system, step)
        except story.NotReadyError as error:
            status.update(label="Not ready", state="error")
            st.warning(str(error))
            return
        except LLMError as error:
            status.update(label="Model call failed", state="error")
            st.error(f"Model call failed: {error}")
            return
        status.update(label=f"Done: {step.title}", state="complete")
    context.log(step.title, lines, (start, system.tracer.count()))
    st.rerun()


def _metrics(system) -> None:
    deals = components.segment_deals(system)
    lessons = [lesson for lesson in system.lessons.all() if lesson.segment == st.session_state.segment]
    uses = [use for use in system.lessons.uses() if use.lesson_id in {lesson.id for lesson in lessons}]
    attention = sum(1 for deal in deals if components.needs_attention(deal))
    columns = st.columns(4)
    columns[0].metric("Deals", len(deals))
    columns[1].metric("Waiting for approval", len(system.approvals.pending()))
    columns[2].metric("Need attention", attention)
    columns[3].metric("Lessons learned · used", f"{len(lessons)} · {len(uses)}")
    st.caption(f"Showing the **{st.session_state.segment}** segment · switch in the sidebar")
