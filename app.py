"""Revenue Agents UI.

    streamlit run app.py

A live world runs behind the pages: leads, replies, calls and signatures
arrive on their own, the agents act within the rules, and only what the rules
send to you waits in the Inbox. It uses the same state folder as run_demo.py
and run_world.py (state/demo); one background runner delivers for it.
"""

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from revenue_agents.ui import components, context, live
from revenue_agents.ui.pages import agent_trace, ask, deal_detail, inbox, pipeline, playbook, team, today

ASSETS = Path(__file__).resolve().parent / "assets"

st.set_page_config(page_title="Revenue Agents · Cool AI", page_icon=str(ASSETS / "icon.svg"), layout="wide")
st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))
load_dotenv()
context.init_session()
components.inject_css()

desk = context.get_desk()
system = desk.system()
waiting = len(system.approvals.pending())
if desk.world.exists():
    # The world's model is what runs. A new browser session (or run_world.py) mustn't show a different one.
    st.session_state.llm_mode = desk.world.read().model

st.session_state.pages = {
    "today": st.Page(today.render, title="Today", icon=":material/today:", url_path="today", default=True),
    "inbox": st.Page(inbox.render, title=f"Inbox · {waiting}" if waiting else "Inbox", icon=":material/inbox:",
                     url_path="inbox"),
    "pipeline": st.Page(pipeline.render, title="Pipeline", icon=":material/view_kanban:", url_path="pipeline"),
    "deal": st.Page(deal_detail.render, title="Deal room", icon=":material/person_search:", url_path="deal"),
    "team": st.Page(team.render, title="Team", icon=":material/groups:", url_path="team"),
    "playbook": st.Page(playbook.render, title="Playbook", icon=":material/menu_book:", url_path="playbook"),
    "ask": st.Page(ask.render, title="Ask", icon=":material/chat:", url_path="ask"),
    "trace": st.Page(agent_trace.render, title="Agent trace", icon=":material/account_tree:", url_path="trace"),
}
pages = st.session_state.pages


def _model_changed() -> None:
    if desk.world.exists():
        desk.world.set_model(st.session_state.llm_mode)


@st.dialog("Start the world over?", icon=":material/restart_alt:")
def _confirm_restart() -> None:
    st.write("This deletes the world and all its state (deals, approvals, lessons, audit log) and starts the clock "
             "again on 1 October. The kill switch keeps its current setting.")
    cancel, confirm = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if confirm.button("Yes, start over", type="primary", width="stretch"):
        if live.restart(desk, context.state_dir(), st.session_state.llm_mode):
            st.rerun()
        st.error("The desk is still busy: try again in a moment.")


with st.sidebar:
    st.segmented_control("Show", ["all", *system.segments.names()], key="segment", required=True,
                         help="Which segment's deals to show. Each segment is one YAML config pack; the agents are "
                              "the same.")
    st.segmented_control("Model", ["scripted", "claude"], key="llm_mode", required=True, on_change=_model_changed,
                         help="scripted: pre-written agent decisions for the mock data, played through the real tools "
                              "and rules. claude: Claude picks the tools (needs ANTHROPIC_API_KEY with credit).")
    if st.session_state.llm_mode == "scripted":
        st.badge("Scripted decisions", icon=":material/science:", color="orange")
    else:
        st.badge("Live Claude", icon=":material/bolt:", color="green")
    if desk.world.exists():
        auto = st.toggle("Pause the clock when something needs you", value=desk.world.read().auto_pause,
                         help="The world waits for your decision instead of running on. Your decisions are "
                              "delivered at once either way.")
        if auto != desk.world.read().auto_pause:
            desk.world.set_auto_pause(auto)
    st.text_input("Reviewer (you)", key="reviewer", help="Recorded on every decision you make.")
    context.remember_settings()
    if waiting:
        st.warning(f"{waiting} waiting for you", icon=":material/front_hand:")
    if system.kill_switch.is_engaged():
        st.error(f"Agents paused: {system.kill_switch.reason()}", icon=":material/front_hand:")
    if desk.world.exists() and not live.runner_is_ours(context.state_dir()):
        st.caption("Another runner (e.g. run_world.py) delivers the world for this folder.")
    if desk.world.exists() and st.button("Start over", icon=":material/restart_alt:", type="tertiary"):
        _confirm_restart()

st.navigation({
    "Desk": [pages["today"], pages["inbox"]],
    "Deals": [pages["pipeline"], pages["deal"]],
    "Agents": [pages["team"], pages["playbook"], pages["ask"], pages["trace"]],
}).run()
