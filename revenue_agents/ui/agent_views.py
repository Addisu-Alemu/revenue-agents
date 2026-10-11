"""Seeing the agents work: live progress, an agent map that lights up, and per-agent scorecards.

All three are built from the agent trace (tracing.py), so they show what really ran,
not what the page claims.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import streamlit as st

from revenue_agents.storage import utc_now
from revenue_agents.tracing import Span

AGENTS = {  # name: (label, uses AI?)
    "research": ("Research", False), "scoring": ("Scoring", False), "outreach": ("Outreach", True),
    "reply_triage": ("Reply triage", True), "call_brief": ("Call brief", False), "call_intel": ("Call intel", True),
    "deal_desk": ("Deal desk", False),
}


def label(name: str) -> str:
    return f"{AGENTS.get(name, (name, False))[0]} agent"


def kind(name: str) -> str:
    return "AI" if AGENTS.get(name, ("", False))[1] else "code"


def headline(name: str, output: Any) -> str:
    """One line saying what the agent produced."""
    o = output or {}
    if name == "research":
        signals = ", ".join(s["type"] for s in o.get("signals", [])) or "no signals"
        return f"{o.get('locations')} locations · {signals}"
    if name == "scoring":
        return f"{o.get('total')} · Tier {o.get('tier')}"
    if name == "outreach":
        lessons = len(o.get("lessons_used", []))
        return "email drafted" + (f" · uses {lessons} lesson(s)" if lessons else "")
    if name == "reply_triage":
        flags = ", ".join(o.get("flags", [])) or "no flags"
        return f"{o.get('classification', {}).get('intent')} · {flags}"
    if name == "call_brief":
        return f"brief ready · {len(o.get('questions', []))} questions · {len(o.get('lessons', []))} lesson(s)"
    if name == "call_intel":
        fields = o.get("fields", {})
        return f"budget {fields.get('budget_usd')} · signer {fields.get('decision_maker')} · {len(o.get('lessons', []))} lesson(s)"
    if name == "deal_desk":
        return f"{o.get('io_number')} drafted" if not o.get("errors") else f"blocked: {'; '.join(o['errors'])}"
    return "done"


def agent_runs(spans: list[Span]) -> list[tuple[Span, list[Span]]]:
    """Each agent step with the model calls it made (model calls are recorded just before their agent finishes)."""
    runs, pending = [], []
    for span in spans:
        if span.kind == "llm":
            pending.append(span)
        elif span.kind == "agent":
            runs.append((span, pending))
            pending = []
    return runs


def _cost(calls: list[Span]) -> float:
    return sum(call.data.get("cost_usd") or 0 for call in calls)


def _timing(agent: Span, calls: list[Span]) -> str:
    seconds = f"{(agent.duration_ms or 0) / 1000:.1f} s"
    cost = _cost(calls)
    return seconds + (f" · ${cost:.4f}" if cost else "")


# --- 1. Progress: live while running, and the same view afterwards -----------------------------------


def _policy_line(data: dict[str, Any]) -> str | None:
    decision = data["decision"].split(" ")[0]
    if decision == "auto":
        return None  # shown as part of the agent's result; only decisions a human should notice are listed
    icon = ":material/front_hand:" if decision == "needs_approval" else ":material/block:"
    color = "orange" if decision == "needs_approval" else "red"
    return f"{icon} :{color}[**Policy: {data['decision'].replace('_', ' ')}**] · {data['reason']}"


def _agent_done_line(span: Span, calls: list[Span]) -> str:
    if "error" in span.data:
        return f":material/error: **{label(span.name)}** ({kind(span.name)}) · :red[{span.data['error']}]"
    return (f":material/check_circle: **{label(span.name)}** ({kind(span.name)}) · {headline(span.name, span.data.get('output'))}"
            f" <span class='ra-muted'>· {_timing(span, calls)}</span>")


def progress_view(spans: list[Span]) -> None:
    """The agents that worked on one event, their results, and anything that needs a human."""
    pending: list[Span] = []
    for span in spans:
        if span.kind == "llm":
            pending.append(span)
        elif span.kind == "agent":
            st.markdown(_agent_done_line(span, pending), unsafe_allow_html=True)
            for call in pending:
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;:material/psychology: <span class='ra-muted'>model call "
                            f"`{call.data['schema']}` · {call.data['model']}</span>", unsafe_allow_html=True)
            pending = []
        elif span.kind == "policy" and (line := _policy_line(span.data)):
            st.markdown(line)
        elif span.kind == "stage" and span.data["from"] != span.data["to"]:
            st.markdown(f":material/trending_flat: :violet[Stage {span.data['from']} → {span.data['to']}]")


class LiveProgress:
    """Draws each step inside an st.status box as it happens: "working…", then the result."""

    def __init__(self) -> None:
        self._agent_slot: dict[str, Any] = {}
        self._model_slot: Any = None
        self._calls: list[Span] = []

    def __call__(self, step: str, name: str, data: dict[str, Any], duration_ms: float | None) -> None:
        if step == "agent_start":
            self._agent_slot[name] = st.empty()
            self._agent_slot[name].markdown(f":material/progress_activity: **{label(name)}** ({kind(name)}) · working…")
        elif step == "llm_start":
            self._model_slot = st.empty()
            self._model_slot.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;:material/psychology: asking the model "
                                      f"({data['model']})…")
        elif step == "llm":
            call = Span(event_id="", at=utc_now(), kind="llm", name=name, duration_ms=duration_ms, data=data)
            self._calls.append(call)
            if self._model_slot is not None:
                cost = f" · ${data['cost_usd']:.4f}" if data.get("cost_usd") else ""
                self._model_slot.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;:material/psychology: model answered "
                                          f"<span class='ra-muted'>· {(duration_ms or 0) / 1000:.1f} s{cost}</span>",
                                          unsafe_allow_html=True)
        elif step == "agent":
            span = Span(event_id="", at=utc_now(), kind="agent", name=name, duration_ms=duration_ms, data=data)
            slot = self._agent_slot.pop(name, None) or st.empty()
            slot.markdown(_agent_done_line(span, self._calls), unsafe_allow_html=True)
            self._calls = []
        elif step == "policy" and (line := _policy_line(data)):
            st.markdown(line)
        elif step == "stage" and data["from"] != data["to"]:
            st.markdown(f":material/trending_flat: :violet[Stage {data['from']} → {data['to']}]")


# --- 2. Agent map -------------------------------------------------------------------------------------

ACTIVE, IDLE = "#4F46E5", "#D1D5DB"


def agent_map(spans: list[Span], waiting_for_approval: int) -> str:
    """A Graphviz diagram of the system. Agents that worked on these steps light up with their result."""
    runs = {agent.name: (agent, calls) for agent, calls in agent_runs(spans)}
    policies = [span for span in spans if span.kind == "policy"]
    executed = any(span.kind == "action" for span in spans)
    lessons = any(span.kind == "action" and span.name in ("record_lesson", "prepare_call_brief", "send_email")
                  for span in spans)

    def node(node_id: str, text: str, active: bool, shape: str = "box") -> str:
        style = (f'style="rounded,filled", fillcolor="{ACTIVE}", fontcolor="white", color="{ACTIVE}"' if active
                 else f'style="rounded", color="{IDLE}", fontcolor="#6B7280"')
        return f'  {node_id} [label="{text}", shape={shape}, {style}];'

    lines = ["digraph agents {", '  rankdir=TB; bgcolor="transparent"; nodesep=0.25; ranksep=0.35;',
             '  node [fontname="Helvetica", fontsize=10, margin="0.12,0.06"]; edge [color="#9CA3AF", arrowsize=0.6];',
             node("orchestrator", "Orchestrator\\nowns deal state", bool(spans))]
    lines.append("  subgraph cluster_agents { label=\"Agents\"; fontname=\"Helvetica\"; fontsize=10; color=\"#E5E7EB\";")
    for name, (title, uses_ai) in AGENTS.items():
        agent = runs.get(name)
        text = f"{title}\\n({'AI' if uses_ai else 'code'})"
        if agent:
            text += "\\n" + headline(name, agent[0].data.get("output")).replace('"', "'")[:38]
        lines.append("  " + node(name, text, agent is not None))
    lines.append("  }")
    decisions = sorted({span.data["decision"].split(" ")[0].replace("_", " ") for span in policies})
    lines.append(node("policy", "Gateway + policy\\n" + (", ".join(decisions) if decisions else "(code, not AI)"),
                      bool(policies), "box"))
    lines.append(node("approvals", f"Approval queue\\n{waiting_for_approval} waiting", waiting_for_approval > 0))
    lines.append(node("crm", "HubSpot (mock)\\nsystem of record", executed, "cylinder"))
    lines.append(node("lessons", "Lesson store", lessons, "cylinder"))
    lines += [f"  orchestrator -> {name};" for name in AGENTS]
    lines += [f"  {name} -> policy [style=dashed];" for name in AGENTS]
    lines += ["  policy -> approvals [label=\" high risk\", fontsize=8];", "  policy -> crm [label=\" low risk\", fontsize=8];",
              "  crm -> lessons [style=dotted, arrowhead=none];", "}"]
    return "\n".join(lines)


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,·") + "…"


# --- 3. Scorecards ----------------------------------------------------------------------------------


def scorecards(spans: list[Span]) -> None:
    """One card per agent: what it is, how often it ran, model calls, time, cost and its latest result."""
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "calls": 0, "ms": 0.0, "cost": 0.0, "last": None})
    for agent, calls in agent_runs(spans):
        entry = stats[agent.name]
        entry["runs"] += 1
        entry["calls"] += len(calls)
        entry["ms"] += agent.duration_ms or 0
        entry["cost"] += _cost(calls)
        entry["last"] = agent

    names = list(AGENTS)
    for row in range(0, len(names), 4):
        for column, name in zip(st.columns(4), names[row:row + 4]):
            title, uses_ai = AGENTS[name]
            entry = stats.get(name)
            with column, st.container(border=True):
                st.markdown(f"**{title}** &nbsp; " + (":violet-badge[AI]" if uses_ai else ":gray-badge[code]"))
                if not entry:
                    st.caption("Hasn't run yet")
                    continue
                average = entry["ms"] / entry["runs"] / 1000
                st.markdown(f"Runs **{entry['runs']}**" + (f" · model calls **{entry['calls']}**" if uses_ai else ""))
                st.caption(f"avg {average:.2f} s" + (f" · ${entry['cost']:.4f}" if entry["cost"] else ""))
                st.caption("Last: " + headline(name, entry["last"].data.get("output")))
