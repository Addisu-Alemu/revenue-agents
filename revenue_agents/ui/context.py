"""Shared UI state: settings, the live world, and the system it runs.

The world and the system live in the state folder (the same folder the
terminal demo uses), so a page refresh never loses track, and every page reads
the same deals. The world itself is delivered by a background runner (live.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from revenue_agents.demo import DEMO_STATE
from revenue_agents.models import Deal
from revenue_agents.system import System
from revenue_agents.ui import live
from revenue_agents.world import Desk

DEFAULTS = {"segment": "all", "llm_mode": "scripted", "reviewer": "you@cool.example"}

# Sidebar settings. Streamlit can drop a widget's value when you switch pages, so each one has a
# saved copy that is restored before the widget is drawn, and refreshed after.
SETTINGS = ("segment", "llm_mode", "reviewer")


def init_session() -> None:
    for key in SETTINGS:
        st.session_state.setdefault(f"saved_{key}", DEFAULTS[key])
        if key not in st.session_state:  # first visit, or Streamlit dropped it on a page change
            st.session_state[key] = st.session_state[f"saved_{key}"]


def remember_settings() -> None:
    """Call after the sidebar widgets: keeps the user's choices across pages."""
    for key in SETTINGS:
        st.session_state[f"saved_{key}"] = st.session_state[key]


def state_dir() -> Path:
    return Path(os.environ.get("REVENUE_AGENTS_STATE_DIR", DEMO_STATE))


def get_desk() -> Desk:
    return live.desk(state_dir())


def get_system() -> System:
    return get_desk().system()


def in_segment(deal: Deal) -> bool:
    return st.session_state.segment in ("all", deal.segment)


def reviewer() -> str:
    return st.session_state.reviewer.strip() or DEFAULTS["reviewer"]
