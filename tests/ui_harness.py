"""Renders one UI page by name (st.session_state.page), for Streamlit's AppTest."""

import streamlit as st

from revenue_agents.ui import components, context
from revenue_agents.ui.pages import agent_trace, deal_detail, inbox, pipeline, team, today

PAGES = {"today": today.render, "inbox": inbox.render, "pipeline": pipeline.render, "deal": deal_detail.render,
         "team": team.render, "trace": agent_trace.render}

context.init_session()
components.inject_css()
PAGES[st.session_state.get("page", "today")]()
