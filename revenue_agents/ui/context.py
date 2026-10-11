"""Shared UI state: settings, the system, and running story steps.

The system is rebuilt on every page run from files in the state folder (the
same folder the terminal demo uses), so the UI and `run_demo.py` see the same
deals. Progress is read from the event claims on disk, not from the browser
session, so a page refresh never loses track.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from revenue_agents import story
from revenue_agents.demo import DEMO_STATE
from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.system import System, build_system, make_llm, reset_state

DEFAULTS = {"segment": "brand", "llm_mode": "fake", "reviewer": "you@cool.example"}


@dataclass
class LogEntry:
    title: str
    lines: list[str]
    trace: tuple[int, int] | None = None  # which recorded trace steps belong to this entry


# Sidebar settings. Streamlit can drop a widget's value when you switch pages, so each one has a
# saved copy that is restored before the widget is drawn, and refreshed after.
SETTINGS = ("segment", "llm_mode", "reviewer")


def init_session() -> None:
    st.session_state.setdefault("log", [])
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


def get_system() -> System:
    return build_system(make_llm(st.session_state.llm_mode), state_dir=state_dir())


def reset() -> None:
    reset_state(state_dir())  # keeps the kill switch
    st.session_state.log = []


def log(title: str, lines: list[str], trace: tuple[int, int] | None = None) -> None:
    st.session_state.log.insert(0, LogEntry(title, lines, trace))


# --- Story progress ---------------------------------------------------------------------


def steps(system: System) -> list[story.Step]:
    return story.plan(system.crm.leads(), st.session_state.segment)


def is_done(system: System, step: story.Step) -> bool:
    events = ActionClaims(state_dir() / "events")
    if step.kind == "lead":
        return events.exists(story.lead_event(step.lead).id)
    if step.kind == "reply":
        return events.exists(story.reply_event(step.reply).id)
    if step.kind == "call":
        return events.exists(f"call:{step.transcript.stem}")
    if step.kind == "signature":
        return any(path.name.startswith("esign%3AENV-IO") for path in _event_files()) or _nothing_to_sign(system, step)
    return any(path.name.startswith("sla%3A") for path in _event_files())


def next_step(system: System) -> story.Step | None:
    return next((step for step in steps(system) if not is_done(system, step)), None)


def run_step(system: System, step: story.Step) -> list[str]:
    return story.run(system, step)


def _nothing_to_sign(system: System, step: story.Step) -> bool:
    """The IO was rejected by a human or blocked by validation: the signature step is skipped, not stuck."""
    deal = next((d for d in system.crm.deals() if d.lead_id == step.lead.id), None)
    if deal is None:
        return False
    rejected = any(r.action.type is ActionType.SEND_IO and r.action.deal_id == deal.id
                   and r.status is ApprovalStatus.REJECTED for r in system.approvals.all())
    return rejected or "io_blocked" in deal.flags


def _event_files() -> list[Path]:
    folder = state_dir() / "events"
    return list(folder.glob("*.json")) if folder.exists() else []
