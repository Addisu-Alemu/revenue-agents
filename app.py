"""Revenue Agents UI.

    streamlit run app.py

Uses the same state folder as run_demo.py (state/demo). Every button sends an
event to the orchestrator, so the policy and gateway apply exactly as in the
terminal demo.
"""

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from revenue_agents.ui import components, context
from revenue_agents.ui.pages import approvals, deal_detail, demo_control, pipeline

ASSETS = Path(__file__).resolve().parent / "assets"

st.set_page_config(page_title="Revenue Agents · Cool AI", page_icon=str(ASSETS / "icon.svg"), layout="wide")
st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))
load_dotenv()
context.init_session()
components.inject_css()

st.session_state.pages = {
    "demo": st.Page(demo_control.render, title="Live demo", icon=":material/play_circle:", url_path="demo",
                    default=True),
    "pipeline": st.Page(pipeline.render, title="Pipeline", icon=":material/view_kanban:", url_path="pipeline"),
    "deal": st.Page(deal_detail.render, title="Deal detail", icon=":material/person_search:", url_path="deal"),
    "approvals": st.Page(approvals.render, title="Approvals", icon=":material/fact_check:", url_path="approvals"),
}
pages = st.session_state.pages

with st.sidebar:
    system = context.get_system()
    st.segmented_control("Segment", system.segments.names(), key="segment", required=True,
                         help="Which segment's story to run. Each segment is one YAML config pack.")
    st.segmented_control("Model", ["fake", "claude"], key="llm_mode", required=True,
                         help="fake: scripted answers for the mock data. claude: the real model (needs API credit).")
    if st.session_state.llm_mode == "fake":
        st.badge("Scripted model answers", icon=":material/science:", color="orange")
    else:
        st.badge("Live Claude", icon=":material/bolt:", color="green")
    st.text_input("Reviewer (you)", key="reviewer", help="Recorded on every approval decision.")
    waiting = len(system.approvals.pending())
    if waiting:
        st.warning(f"{waiting} waiting for approval", icon=":material/front_hand:")
    if system.kill_switch.is_engaged():
        st.error(f"Kill switch ON: {system.kill_switch.reason()}", icon=":material/power_settings_new:")

st.navigation({
    "Run": [pages["demo"]],
    "Sales": [pages["pipeline"], pages["deal"]],
    "Human in the loop": [pages["approvals"]],
}).run()
