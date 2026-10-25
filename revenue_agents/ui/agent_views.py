"""Seeing the agents work: tool chips, one line per agent run, and per-agent scorecards.

All of it is built from the agent trace (tracing.py), so it shows what really
ran: which tools each agent chose, in order, and what the hook and the gateway
did with each call.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape

import streamlit as st

from revenue_agents.team import LABELS
from revenue_agents.tracing import Span
from revenue_agents.ui.feed import Run, ToolStep, runs_in

TONES = {"read": ("gray", ""), "done": ("green", " ✓"), "fix": ("gray", " · sent back to fix"),
         "asked": ("orange", " · asks you"), "refused": ("red", " · refused")}
OUTCOME_COLORS = {"on its own": "green", "asked you": "orange", "refused": "red", "read only": "gray",
                  "failed": "red"}


def chips(steps: list[ToolStep]) -> str:
    """The tools a run called, in order, as small badges: grey reads, green done, orange waits for you, red refused."""
    return " ".join(f":{TONES[step.tone][0]}-badge[{step.tool}{TONES[step.tone][1]}]" for step in steps)


CHIP_CLASSES = {"read": "", "done": "ok", "fix": "fix", "asked": "ask", "refused": "no"}


def chips_html(steps: list[ToolStep]) -> str:
    """The same tool chips as HTML, for rows drawn in HTML (monospace, like the mockup)."""
    return "".join(f"<span class='ra-chip {CHIP_CLASSES[step.tone]}'>{escape(step.tool)}{TONES[step.tone][1]}</span>"
                   for step in steps)


def run_line(run: Run) -> str:
    outcome = run.outcome
    return (f"**{run.label}** · {_clean(run.title)} &nbsp; :{OUTCOME_COLORS[outcome]}-badge[{outcome}]  \n"
            f"{chips(run.tools)}")


def run_details(run: Run, footer: bool = True) -> None:
    """Each tool call with the hook's verdict and rule, and what the agent read back."""
    for step in run.tools:
        color, mark = TONES[step.tone]
        rule = f" · rule `{step.rule}`" if step.rule and step.rule != "read_only" else ""
        st.markdown(f":{color}-badge[{step.tool}{mark}]{rule}  \n<span class='ra-muted'>{_clean(step.result)[:300]}"
                    f"</span>", unsafe_allow_html=True)
    if not footer:
        return
    if run.summary:
        st.caption(f"The agent: “{_clean(run.summary)}”")
    st.caption(f"{run.model} · {run.ms / 1000:.1f} s" + (f" · ${run.cost:.4f}" if run.cost is not None else ""))


def scorecards(spans: list[Span]) -> None:
    """One card per AI agent: runs, tool calls, what waited for a human, what was refused, time and cost."""
    runs: dict[str, list[Run]] = defaultdict(list)
    by_event: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        by_event[span.event_id].append(span)
    for event_spans in by_event.values():
        for run in runs_in(event_spans):
            runs[run.agent].append(run)
    agents = [name for name in ("research", "outreach", "reply", "call_intel") if runs.get(name)]
    if not agents:
        st.caption("No agent has worked yet.")
        return
    for column, name in zip(st.columns(len(agents)), agents):
        done = runs[name]
        steps = [step for run in done for step in run.tools]
        cost = sum(run.cost or 0 for run in done)
        with column, st.container(border=True):
            st.markdown(f"**{LABELS[name]}**")
            st.markdown(f"{len(done)} runs · {len(steps)} tool calls  \n"
                        f":orange-badge[{sum(s.tone == 'asked' for s in steps)} asked you] "
                        f":red-badge[{sum(s.tone == 'refused' for s in steps)} refused]")
            st.caption(f"{sum(run.ms for run in done) / 1000:.1f} s · ${cost:.4f} · {done[-1].model}")


def _clean(text: str) -> str:
    """Streamlit reads $...$ as maths and [..] as links inside badges: show them as text."""
    return text.replace("$", "\\$").replace("\n", " ")
