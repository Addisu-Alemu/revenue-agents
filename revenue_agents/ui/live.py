"""The live world in the UI: one desk and one background runner per state folder, and the controls people use.

Your decisions (approve, reject, coach, change terms, run now, resume) go to
the world's inbox and are delivered next, even while the clock is paused, by
the same runner that delivers everything else. So you and the agents never
work on the same deal at the same moment. Pausing the agents (the kill switch)
is the one control that acts at once: nothing may wait in line to stop them.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import streamlit as st

from revenue_agents.events import approval_event, terms_event
from revenue_agents.guardrails.approvals import ApprovalRequest, ApprovalStatus
from revenue_agents.storage import set_clock
from revenue_agents.system import System, reset_state
from revenue_agents.world import Desk, Runner, World

PACE = 0.5  # scripted mode: seconds before each tool call, so people can watch the agents work
WAIT = 8.0  # how long a click waits for the desk to finish it before the page moves on


@st.cache_resource(show_spinner=False)
def _desk(state_dir: str) -> Desk:
    return Desk(Path(state_dir), pace=PACE)


@st.cache_resource(show_spinner=False)
def _runner(state_dir: str) -> Runner | None:
    """Start delivering for this folder, unless another runner (e.g. run_world.py) already does, or the UI was told
    not to (REVENUE_AGENTS_RUNNER=off: tests deliver step by step themselves)."""
    if os.environ.get("REVENUE_AGENTS_RUNNER") == "off":
        return None
    runner = Runner(_desk(state_dir))
    return runner if runner.start() else None


def desk(state_dir: Path) -> Desk:
    current = _desk(str(state_dir))
    set_clock(current.world.now)
    _runner(str(state_dir))
    return current


def runner_is_ours(state_dir: Path) -> bool:
    runner = _runner(str(state_dir))
    return bool(runner and runner.alive())


# --- Your decisions -----------------------------------------------------------------------


def decide(world: World, system: System, request: ApprovalRequest, approved: bool, reviewer: str, note: str | None,
           save_lesson: bool = False, edits: dict[str, str] | None = None) -> bool:
    """Send a decision to the desk and wait (a few seconds at most) until it is done. True if it was."""
    verb = "Approved" if approved else ("Coached" if note else "Rejected")
    world.submit(approval_event(request.id, approved, reviewer, note, save_lesson, edits),
                 f"{verb} by you: {request.action.summary}")
    return _wait(lambda: system.approvals.get(request.id).status is not ApprovalStatus.PENDING and not _yours(world))


def change_terms(world: World, deal_id: str, changes: dict, reviewer: str) -> bool:
    world.submit(terms_event(deal_id, changes, reviewer), f"You changed the terms on {deal_id}")
    return _wait(lambda: not _yours(world))


def run_now(world: World, request: ApprovalRequest) -> bool:
    world.command("run_now", f"You ran an approved action: {request.action.summary}", request_id=request.id)
    return _wait(lambda: not _yours(world))


def coaching(world: World, request: ApprovalRequest, keep: bool, by: str) -> bool:
    """Keep a reviewer's note as a lesson, or keep it for that one email only. Goes through the desk like a decision."""
    name = "keep_coaching" if keep else "drop_coaching"
    label = "You kept your note as a lesson" if keep else "You kept your note for that email only"
    world.command(name, f"{label}: {request.note}", request_id=request.id, by=by)
    return _wait(lambda: not _yours(world))


def pause_agents(system: System, reason: str, by: str) -> None:
    system.orchestrator.pause(reason, by=by)


def resume_agents(world: World, by: str) -> bool:
    world.command("resume", "You released the agents", by=by)
    return _wait(lambda: not _yours(world))


def restart(desk_: Desk, state_dir: Path, model: str) -> bool:
    """A fresh world. Waits for the desk to finish what it's doing first. The kill switch keeps its setting."""
    desk_.world.pause()
    if not _wait(lambda: not desk_.world.read().working, timeout=30):
        return False
    reset_state(state_dir)
    desk_.start(model=model)
    return True


def _yours(world: World) -> bool:
    state = world.read()
    return any(item.source == "you" for item in state.queue)


def _wait(done: Callable[[], bool], timeout: float = WAIT) -> bool:
    if os.environ.get("REVENUE_AGENTS_RUNNER") == "off":
        return done()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if done():
            return True
        time.sleep(0.2)
    return done()
