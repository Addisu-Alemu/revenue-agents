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
from revenue_agents.ui import agent_views, components, context, desk_views, feed, live
from revenue_agents.world import SPEEDS

SPEED_NAMES = {value: name for name, value in SPEEDS.items()}
DOTS = {"on its own": "#7C3AED", "asked you": "#DC2626", "refused": "#DC2626", "failed": "#DC2626",
        "read only": "#9CA3AF"}
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
    pending = [r for r in system.approvals.pending() if context.in_segment(system.crm.get_deal(r.action.deal_id))]
    acted = feed.acted_on_their_own(system.tracer.spans())
    headline = ("1 decision needs you" if len(pending) == 1 else
                f"{len(pending)} decisions need you" if pending else "Nothing needs you")
    head, buttons = st.columns([3, 3.4], vertical_alignment="bottom")
    with head:
        if state.working:
            status = f"<span class='ra-status busy'>working: {_html(state.working)}</span>"
        elif state.running:
            status = f"<span class='ra-status run'>running · {SPEED_NAMES.get(state.speed, f'{state.speed} s/s')}</span>"
        elif state.paused_for:
            status = f"<span class='ra-status ask'>clock paused: {_html(state.paused_for)}</span>"
        else:
            status = "<span class='ra-status'>clock paused</span>"
        if paused_agents:
            status += " <span class='ra-status stop'>agents paused</span>"
        st.markdown(f"<div class='ra-date'>{state.now:%A %d %B · %H:%M} UTC &nbsp; {status}</div>"
                    f"<h1 class='ra-headline'>{headline}</h1>"
                    f"<p class='ra-lede'>So far your agents took {acted} action{'s' if acted != 1 else ''} on their "
                    "own. Everything they did is below, with the tools they used.</p>", unsafe_allow_html=True)
    with buttons, st.container(horizontal=True, horizontal_alignment="right", vertical_alignment="center"):
        speed = st.segmented_control("Speed", list(SPEEDS), default=SPEED_NAMES.get(state.speed), key="speed",
                                     label_visibility="collapsed")
        if speed and SPEEDS[speed] != state.speed:
            desk.world.set_speed(SPEEDS[speed])
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
        if paused_agents:
            if st.button("Release agents", icon=":material/play_circle:", type="primary"):
                live.resume_agents(desk.world, context.reviewer())
                st.rerun(scope="fragment")
        elif st.button("Pause all agents", icon=":material/pause_circle:", key="ra-kill",
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

    left, right = st.columns([3, 1.35], gap="large")
    with left:
        _needs_you(system, spans, pending)
        _what_they_did(desk, system, spans)
    with right:
        _working_now(desk)
        _coming_up(desk, system)
        _pipeline(system)
        _learned(system)


def _needs_you(system, spans, pending) -> None:
    title, link = st.columns([4, 1], vertical_alignment="bottom")
    title.markdown("#### Needs you")
    if pending and link.button("Open inbox", key="open-inbox", type="tertiary"):
        st.switch_page(st.session_state.pages["inbox"])
    if not pending:
        st.caption("Nothing waits for you. The agents are working within the rules.")
        return
    now = context.get_desk().world.now()
    for request in sorted(pending, key=lambda r: r.created_at):
        deal = system.crm.get_deal(request.action.deal_id)
        lead = system.crm.get_lead(deal.lead_id)
        tier = f"Tier {deal.score.tier.value} · {deal.score.total:g}" if deal.score else "no tier"
        _, rule = desk_views.stopped_by(spans, request)
        with st.container(border=True):
            text, button = st.columns([6, 1], vertical_alignment="top")
            text.markdown(
                f"<div class='ra-need'><span class='ra-dot' style='background:#DC2626'></span><div>"
                f"<div class='ra-meta'>{_html(lead.company)} · {tier} · waiting {_ago(now - request.created_at)}</div>"
                f"<div class='ra-need-title'>{_html(request.action.summary)}</div>"
                f"<div class='ra-need-why'>{_html(desk_views.context_line(system, request))}</div>"
                f"<span class='ra-rule'>● Rule: {_html(rule)}</span></div></div>", unsafe_allow_html=True)
            if button.button("Review", key=f"review-{request.id}", type="tertiary"):
                st.session_state.inbox_selected = request.id
                st.switch_page(st.session_state.pages["inbox"])


def _what_they_did(desk, system, spans) -> None:
    with st.container(border=True):
        st.markdown("<div class='ra-feed-head'><h4>What your agents did</h4><span>"
                    "<i style='background:#7C3AED'></i>on its own <i style='background:#DC2626'></i>asked you "
                    "<i style='background:#111827'></i>you</span></div>", unsafe_allow_html=True)
        rows = _rows(system, [entry for entry in feed.entries(desk.world, spans) if _shown(entry)])
        if not rows:
            st.caption("Nothing yet. Press Play (or Next arrival): the first lead arrives at 09:15.")
            return
        st.markdown("".join(rows[:40]), unsafe_allow_html=True)


def _rows(system, entries) -> list[str]:
    """One row per agent run (newest first), plus your decisions and what arrived without an agent run."""
    companies = {deal.id: system.crm.get_lead(deal.lead_id).company for deal in system.crm.deals()}
    rows = []
    for entry in entries:
        if entry.status == "held":
            rows.append(_row(entry.at, "#F59E0B", "Kill switch", "", f"{entry.label}: waits until you release the "
                                                                    "agents. Nothing is lost.", ""))
            continue
        for run in reversed(entry.runs):
            color = DOTS.get(run.outcome, "#7C3AED")
            rows.append(_row(run.at, color, run.label, companies.get(run.deal, ""), run.summary or run.title,
                             agent_views.chips_html(run.tools), run.outcome))
        if entry.runs:
            continue
        if entry.source == "you":
            rows.append(_row(entry.at, "#111827", "You", "", entry.label, ""))
        else:
            detail = next((step for step in entry.steps if step and not step.startswith("Daily digest")), "")
            who = {"scheduler": "Scheduler", "timer": "Timer"}.get(entry.source, "Arrived")
            rows.append(_row(entry.at, "#9CA3AF", who, "", f"{entry.label}" + (f": {detail}" if detail else ""), ""))
    return rows


def _row(at, color: str, who: str, company: str, text: str, chips: str, outcome: str = "") -> str:
    chip = f"<span class='ra-outcome'>{outcome}</span>" if outcome else ""
    where = f" <span class='ra-company'>{_html(company)}</span>" if company else ""
    tools = f"<div class='ra-chips'>{chips}</div>" if chips else ""
    return (f"<div class='ra-row'><span class='ra-time'>{at:%a %H:%M}</span><span class='ra-dot' "
            f"style='background:{color}'></span><div><b>{_html(who)}</b>{where} {chip}"
            f"<div class='ra-row-text'>{_html(text)}</div>{tools}</div></div>")


def _working_now(desk) -> None:
    board = desk.live.read()
    with st.container(key="ra-working"):
        st.markdown("##### Working now")
        if not board:
            state = desk.world.read()
            upcoming = [item.at for item in state.queue if item.at > state.now]
            nxt = f" Next arrival {min(upcoming):%a %H:%M}." if upcoming else ""
            st.markdown(f"<p>All caught up.{nxt}</p>", unsafe_allow_html=True)
            return
        steps = [feed.ToolStep(step["tool"], step.get("verdict") or "", step.get("outcome"), "", "",
                               step.get("result") or "") for step in board.get("steps", [])]
        agent = (f"<p><b>{LABELS.get(board['agent'], board['agent'])}</b> · {_html(board.get('title') or '')}</p>"
                 if board.get("agent") else "")
        calling = f"<p class='ra-calling'>calling {_html(board['tool'])}…</p>" if board.get("tool") else ""
        last = f"<p class='ra-calling'>{_html(steps[-1].result[:160])}</p>" if steps else ""
        st.markdown(f"<p><b>{_html(board.get('item', ''))}</b></p>{agent}"
                    f"<div class='ra-chips'>{agent_views.chips_html(steps)}</div>{last}{calling}",
                    unsafe_allow_html=True)


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
    deals = [deal for deal in system.crm.deals() if context.in_segment(deal)]
    counts = [(name, sum(deal.stage in stages for deal in deals)) for name, stages in PIPELINE]
    tiles = "".join(f"<div><b>{count}</b><span>{name}</span></div>" for name, count in counts if count)
    proposal = [d for d in deals if d.stage in (Stage.PROPOSAL, Stage.SIGNATURE) and d.fields.budget_usd]
    money = (f"<p class='ra-muted'>&#36;{sum(d.fields.budget_usd for d in proposal):,} in proposals and signatures"
             "</p>" if proposal else "")
    with st.container(border=True):
        title, link = st.columns([3, 1.4], vertical_alignment="bottom")
        title.markdown("##### Pipeline")
        if link.button("Deals", key="open-pipeline", type="tertiary"):
            st.switch_page(st.session_state.pages["pipeline"])
        st.markdown(f"<div class='ra-tiles'>{tiles or '<p class=ra-muted>No deals yet.</p>'}</div>{money}",
                    unsafe_allow_html=True)


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


def _html(text: str) -> str:
    """Text shown inside HTML: no markup, dollar signs as text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace("$", "&#36;")
