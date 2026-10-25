"""Agent trace: pick anything that arrived and see, step by step, what each agent chose to do and why it was allowed."""

from __future__ import annotations

import streamlit as st

from revenue_agents.ui import components, context


def render() -> None:
    desk = context.get_desk()
    system = desk.system()
    components.page_header("Agent trace", "Proof the agents work: for any event, each agent's tool calls in the "
                                          "order it chose them, the rule that allowed, deferred or refused each "
                                          "one, what the gateway executed, and how the deal moved.")
    spans = system.tracer.spans()
    if not spans:
        st.info("No trace yet. Start the world on the Today page.", icon=":material/info:")
        return

    events: dict[str, list] = {}
    for span in spans:
        events.setdefault(span.event_id, []).append(span)
    labels = {event_id: d.label for d in desk.world.log() for event_id in d.events}
    ids = list(reversed(events))  # newest first

    def label(event_id: str) -> str:
        steps = events[event_id]
        tools = sum(span.kind == "tool" for span in steps)
        return f"{labels.get(event_id, event_id)} · {tools} tool call(s) · {event_id}"

    busy = next((i for i, event_id in enumerate(ids) if any(span.kind == "tool" for span in events[event_id])), 0)
    event_id = st.selectbox("Event", ids, index=busy, format_func=label)
    steps = events[event_id]
    runs = [span for span in steps if span.kind == "agent" and "tool_calls" in span.data]
    cost = sum(span.data.get("cost_usd") or 0 for span in runs)
    columns = st.columns(4)
    columns[0].metric("Agent runs", len(runs))
    columns[1].metric("Tool calls", sum(span.kind == "tool" for span in steps))
    columns[2].metric("Policy decisions", sum(span.kind == "policy" for span in steps))
    columns[3].metric("Model cost", f"${cost:.4f}")
    components.trace_view(steps)
