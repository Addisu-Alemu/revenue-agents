"""Renders one UI page by name (st.session_state.page), for Streamlit's AppTest."""

import streamlit as st

from revenue_agents.ui import components, context
from revenue_agents.ui.pages import agent_trace, approvals, deal_detail, demo_control, pipeline

PAGES = {"demo": demo_control.render, "trace": agent_trace.render, "pipeline": pipeline.render, "deal": deal_detail.render,
         "approvals": approvals.render}

context.init_session()
components.inject_css()
PAGES[st.session_state.get("page", "demo")]()
