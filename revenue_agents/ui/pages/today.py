"""Today: the agents run the desk, you run the agents.

The world's clock runs; leads, replies, calls and signatures arrive on their
own; the agents act within the rules. This page shows what needs you, who is
working right now, what the agents did and what is coming up. It refreshes
itself every second.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from revenue_agents.models import Stage
from revenue_agents.team import LABELS
from revenue_agents.ui import agent_views, components, context, feed, live
from revenue_agents.world import SPEEDS

SPEED_NAMES = {value: name for name, value in SPEEDS.items()}
PIPELINE = [("New", (Stage.PROSPECT, Stage.ENRICHED, Stage.SCORED)), ("Contacted", (Stage.OUTREACH,)),
            ("Talking", (Stage.REPLIED, Stage.MEETING)), ("Proposal", (Stage.QUALIFIED, Stage.PROPOSAL)),
            ("Signature", (Stage.SIGNATURE,)), ("Won", (Stage.HANDOFF,)), ("Lost", (Stage.CLOSED_LOST,))]


def render() -> None:
    desk = context.get_desk()
    if not desk.world.exists():
        _start_screen(desk)
        return
    _controls(desk)
    _desk_view()


def _start_screen(desk) -> None:
    components.page_header("Today", "The agents run the desk; you run the agents.")
    with st.container(border=True):
        st.markdown("#### Start the world")
        st.markdown("The clock starts on Thursday 1 October, just before the first lead arrives. Leads, replies, "
                    "calls and signatures then arrive on their own, in reaction to what the agents do. You decide "
                    "only what the rules send to you.")
        if st.button("Start", type="primary", icon=":material/play_arrow:"):
            desk.start(model=st.session_state.llm_mode)
            desk.world.play()
            st.rerun()


@st.fragment(run_every=1.0)
def _controls(desk) -> None:
    state = desk.world.read()
    system = desk.system()
    paused_agents = system.kill_switch.is_engaged()
    title, buttons = st.columns([3, 4], vertical_alignment="center")
    with title:
        st.title("Today")
        if state.working:
            status = f":blue-badge[:material/progress_activity: working: {state.working}]"
        elif state.running:
            status = f":green-badge[running · {SPEED_NAMES.get(state.speed, f'{state.speed} s/s')}]"
        else:
            status = ":gray-badge[clock paused]"
        st.markdown(f"**{state.now:%a %d %b %Y · %H:%M} UTC** &nbsp; {status}"
                    + (" &nbsp; :red-badge[agents paused]" if paused_agents else ""))
    with buttons, st.container(horizontal=True, horizontal_alignment="right", vertical_alignment="center"):
        if state.running:
            if st.button("Pause clock", icon=":material/pause:"):
                desk.world.pause()
                st.rerun(scope="fragment")
        elif st.button("Play", type="primary", icon=":material/play_arrow:"):
            desk.world.play()
            st.rerun(scope="fragment")
        if st.button("Next arrival", icon=":material/skip_next:", help="Jump the clock to the next thing that arrives"):
            desk.world.skip()
            st.rerun(scope="fragment")
        speed = st.segmented_control("Speed", list(SPEEDS), default=SPEED_NAMES.get(state.speed), key="speed",
                                     label_visibility="collapsed")
        if speed and SPEEDS[speed] != state.speed:
            desk.world.set_speed(SPEEDS[speed])
        if paused_agents:
            if st.button("Release agents", icon=":material/play_circle:", type="primary"):
                live.resume_agents(desk.world, context.reviewer())
                st.rerun(scope="fragment")
        elif st.button("Pause all agents", icon=":material/front_hand:",
                       help="The kill switch: nothing reaches a customer or the CRM; events wait in order"):
            live.pause_agents(system, "Paused from Today", context.reviewer())
            st.rerun(scope="fragment")
    if state.error:
        st.error(f"The world paused: {state.error}. Fix it (e.g. the API key), then press Play to retry. Nothing that "
                 f"arrived is lost.", icon=":material/error:")
    if paused_agents:
        held = len(system.orchestrator.held_events())
        st.warning(f"All agents paused by you: {system.kill_switch.reason()}. Events keep arriving and wait in order "
                   f"({held} so far). Nothing reaches a customer or the CRM until you release them.",
                   icon=":material/front_hand:")


@st.fragment(run_every=1.0)
def _desk_view() -> None:
    desk = context.get_desk()
    system = desk.system()
    spans = system.tracer.spans()
    pending = [r for r in system.approvals.pending() if context.in_segment(system.crm.get_deal(r.action.deal_id))]
    acted = feed.acted_on_their_own(spans)
    decisions = "1 decision needs you" if len(pending) == 1 else f"{len(pending) or 'No'} decisions need you"
    st.subheader(f"{decisions} · {acted} action{'s' if acted != 1 else ''} the agents took on their own")

    left, right = st.columns([3, 2], gap="large")
    with left:
        _needs_you(system, pending)
        _what_they_did(desk, spans)
    with right:
        _working_now(desk)
        _coming_up(desk, system)
        _pipeline(system)
        _learned(system)


def _needs_you(system, pending) -> None:
    st.markdown("##### Needs you")
    if not pending:
        st.caption("Nothing waits for you. The agents are working within the rules.")
        return
    now = context.get_desk().world.now()
    for request in sorted(pending, key=lambda r: r.created_at):
        deal = system.crm.get_deal(request.action.deal_id)
        lead = system.crm.get_lead(deal.lead_id)
        tier = f"Tier {deal.score.tier.value}" if deal.score else "no tier"
        with st.container(border=True):
            text, button = st.columns([5, 1.3], vertical_alignment="center")
            text.markdown(f"**{request.action.summary}**  \n<span class='ra-muted'>{lead.company} · {tier} · waiting "
                          f"{_ago(now - request.created_at)} · {request.reason}</span>", unsafe_allow_html=True)
            if button.button("Review", key=f"review-{request.id}", icon=":material/arrow_forward:"):
                st.session_state.inbox_selected = request.id
                st.switch_page(st.session_state.pages["inbox"])


def _what_they_did(desk, spans) -> None:
    st.markdown("##### What happened")
    entries = [entry for entry in feed.entries(desk.world, spans) if _shown(entry)]
    if not entries:
        st.caption("Nothing yet. Press Play (or Next arrival): the first lead arrives at 09:15.")
        return
    for entry in entries[:15]:
        with st.container(border=True):
            held = " &nbsp; :orange-badge[waits: agents paused]" if entry.status == "held" else ""
            st.markdown(f"<span class='ra-muted'>{entry.at:%a %H:%M} · {_who(entry.source)}</span> &nbsp; "
                        f"**{_clean(entry.label)}**{held}", unsafe_allow_html=True)
            for run in entry.runs:
                st.markdown(agent_views.run_line(run))
            if entry.steps:
                with st.expander("What the system did", icon=":material/list:"):
                    st.markdown("  \n".join(components.styled_line(line) for line in entry.steps if line))


def _working_now(desk) -> None:
    st.markdown("##### Working now")
    board = desk.live.read()
    with st.container(border=True):
        if not board:
            state = desk.world.read()
            upcoming = [item.at for item in state.queue if item.at > state.now]
            if state.running and upcoming:
                st.caption(f"All caught up. Next arrival {min(upcoming):%a %H:%M}.")
            else:
                st.caption("All caught up.")
            return
        st.markdown(f":material/progress_activity: **{_clean(board.get('item', ''))}**")
        if board.get("agent"):
            st.markdown(f"**{LABELS.get(board['agent'], board['agent'])}** · {_clean(board.get('title') or '')}")
        steps = [feed.ToolStep(step["tool"], step.get("verdict") or "", step.get("outcome"), "", "",
                               step.get("result") or "") for step in board.get("steps", [])]
        if steps:
            st.markdown(agent_views.chips(steps))
            st.caption(_clean(steps[-1].result)[:160])
        if board.get("tool"):
            st.caption(f"calling {board['tool']}…")


def _coming_up(desk, system) -> None:
    st.markdown("##### Coming up")
    now = desk.world.now()
    items = [(item.at, item.label) for item in desk.world.upcoming()]
    for deal in system.crm.deals():
        for activity in system.crm.timeline(deal.id):
            if activity.kind == "meeting_booked":
                when = dt.datetime.fromisoformat(activity.data["when"])
                if when > now:
                    items.append((when, f"Call with {system.crm.get_lead(deal.lead_id).company}"))
    with st.container(border=True):
        if not items:
            st.caption("Nothing scheduled.")
        for when, label in sorted(items)[:6]:
            st.markdown(f"<span class='ra-muted'>{when:%a %d %b %H:%M}</span> &nbsp; {label}", unsafe_allow_html=True)
        st.caption("Timers the scheduler and the agents set. Each one survives a restart.")


def _pipeline(system) -> None:
    st.markdown("##### Pipeline")
    deals = [deal for deal in system.crm.deals() if context.in_segment(deal)]
    st.markdown(" ".join(f":{'violet' if count else 'gray'}-badge[{name} · {count}]" for name, count in
                         ((name, sum(deal.stage in stages for deal in deals)) for name, stages in PIPELINE)))
    proposal = [d for d in deals if d.stage in (Stage.PROPOSAL, Stage.SIGNATURE) and d.fields.budget_usd]
    if proposal:
        st.caption(f"\\${sum(d.fields.budget_usd for d in proposal):,} in proposals and signatures")


def _learned(system) -> None:
    lessons = system.lessons.all()
    used = sum(bool(system.lessons.uses(lesson.id)) for lesson in lessons)
    with st.container(border=True):
        text, link = st.columns([3, 1.2], vertical_alignment="center")
        text.markdown(f":material/psychology: **{len(lessons)} lessons learned · {used} used** in emails and briefs")
        if link.button("Playbook", key="open-playbook", icon=":material/arrow_forward:", type="tertiary"):
            st.switch_page(st.session_state.pages["playbook"])


def _shown(entry: feed.Entry) -> bool:
    if entry.source == "scheduler" and not entry.runs and entry.steps in (["SLA check: 0 stalled deal(s)"],):
        return False
    return True


def _who(source: str) -> str:
    return {"lead": "new lead", "buyer": "from outside", "scheduler": "scheduler", "timer": "timer",
            "you": "you"}.get(source, source)


def _ago(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} days"


def _clean(text: str) -> str:
    return text.replace("$", "\\$")
