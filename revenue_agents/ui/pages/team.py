"""Team: who the agents are, which tools each may use, the rules that decide every call, and the audit log.

Everything here is generated from the code (team.py reads the agents' real
tools; the rules come from the autonomy policy and the segment packs), so
the page can't promise what the system doesn't do.
"""

from __future__ import annotations

import streamlit as st

from revenue_agents import team
from revenue_agents.ui import agent_views, components, context, live

PERMISSION_COLORS = {"no side effects": "gray", "on its own": "green", "always asks you": "orange"}


def render() -> None:
    desk = context.get_desk()
    system = desk.system()
    components.page_header("Your agent team", "Four AI agents, each with one job and only the tools it needs. "
                                              "Whether a tool call runs on its own, waits for you or is refused is "
                                              "decided by the policy in code, never by the agent's prompt.")
    _kill_switch(desk, system)

    st.subheader("The agents")
    model = system.runtime.label
    for left, right in _pairs(team.roster(system)):
        columns = st.columns(2)
        for column, card in zip(columns, (left, right)):
            if card:
                with column, st.container(border=True):
                    _agent(card, model)

    st.subheader("Code skills, on purpose")
    columns = st.columns(len(team.SKILLS))
    for column, (name, job) in zip(columns, team.SKILLS.items()):
        with column, st.container(border=True):
            st.markdown(f":material/functions: **{name}**")
            st.caption(job)
    with st.container(border=True):
        st.markdown(":material/theater_comedy: **Buyer simulator** &nbsp; :gray-badge[demo only]")
        st.caption("Plays each prospect from a hidden persona and answers what the agents actually did: no reply to "
                   "an email that never went out, a call only once a meeting is booked, a signature only on a valid "
                   "IO. It writes only to the mock inbox, calendar and e-sign provider, and doesn't exist in "
                   "production.")

    st.subheader("How each call is decided")
    st.caption("Every tool call passes a PreToolUse hook that asks the policy: allow runs it (once: the action id "
               "is the idempotency key), defer stops the agent and puts the call in your inbox, deny tells the agent "
               "why. The gateway checks again before anything runs.")
    st.dataframe([{"Tool call": rule.applies_to, "Rule": rule.rule, "Decision": rule.decision,
                   "Who can change it": rule.who_changes_it} for rule in team.rules(system)],
                 hide_index=True, width="stretch")

    st.subheader("How they've done")
    agent_views.scorecards(system.tracer.spans())

    st.subheader("Audit log")
    events = list(reversed(system.audit.events()))[:40]
    if not events:
        st.caption("Empty: nothing has happened yet.")
    else:
        st.dataframe([{"When": f"{event.at:%a %d %b %H:%M}", "Who": team.LABELS.get(event.actor, event.actor),
                       "What": event.event.replace("_", " "), "Deal": event.deal_id or "", "Detail": event.detail}
                      for event in events], hide_index=True, width="stretch")


def _agent(card: team.AgentCard, model: str) -> None:
    st.markdown(f":material/smart_toy: **{card.label}** &nbsp; :violet-badge[{model}]")
    st.caption(card.job)
    for tool in card.tools:
        color = PERMISSION_COLORS.get(tool.permission, "orange")
        guard = " · a rule in code can refuse it" if tool.guarded else ""
        st.markdown(f"`{tool.name}` &nbsp; :{color}-badge[{tool.permission}]  \n"
                    f"<span class='ra-muted'>{tool.description}{guard}</span>", unsafe_allow_html=True)


def _kill_switch(desk, system) -> None:
    with st.container(border=True):
        text, button = st.columns([5, 1.5], vertical_alignment="center")
        if system.kill_switch.is_engaged():
            text.markdown(f":material/front_hand: **All agents are paused:** {system.kill_switch.reason()}  \n"
                          f"<span class='ra-muted'>Events keep arriving and wait in order "
                          f"({len(system.orchestrator.held_events())} so far). Approved actions wait too.</span>",
                          unsafe_allow_html=True)
            if button.button("Release", type="primary", icon=":material/play_circle:", width="stretch"):
                live.resume_agents(desk.world, context.reviewer())
                st.rerun()
        else:
            text.markdown(":material/verified_user: **Kill switch** · off  \n<span class='ra-muted'>Pausing stops every "
                          "side effect at once; nothing is lost, and release replays what waited, in order. Also "
                          "`python scripts/kill_switch.py on`.</span>", unsafe_allow_html=True)
            if button.button("Pause all agents", icon=":material/front_hand:", width="stretch"):
                live.pause_agents(system, "Paused from the Team page", context.reviewer())
                st.rerun()


def _pairs(cards: list) -> list[tuple]:
    return [(cards[i], cards[i + 1] if i + 1 < len(cards) else None) for i in range(0, len(cards), 2)]
