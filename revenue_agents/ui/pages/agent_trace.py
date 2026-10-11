"""Agent trace: pick any event and see, step by step, what each agent and the model did."""

from __future__ import annotations

import streamlit as st

from revenue_agents.ui import agent_views, components, context


def render() -> None:
    system = context.get_system()
    components.page_header("Agent trace", "Proof the agents work: each agent's input, the exact prompt sent to the "
                                          "model, its raw answer, what was proposed, and what the policy decided.")
    spans = system.tracer.spans()
    if not spans:
        st.info("No trace yet. Run a step on the Live demo page.", icon=":material/info:")
        return

    st.subheader("The agent team")
    agent_views.scorecards(spans)

    st.subheader("One event, step by step")
    events: dict[str, list] = {}
    for span in spans:
        events.setdefault(span.event_id, []).append(span)
    ids = list(reversed(events))  # newest first

    def label(event_id: str) -> str:
        steps = events[event_id]
        ai_calls = sum(span.kind == "llm" for span in steps)
        return f"{event_id} · {len(steps)} steps · {ai_calls} model call(s)"

    event_id = st.selectbox("Event", ids, format_func=label)
    steps = events[event_id]
    cost = sum(span.data.get("cost_usd") or 0 for span in steps if span.kind == "llm")
    columns = st.columns(4)
    columns[0].metric("Agents", sum(span.kind == "agent" for span in steps))
    columns[1].metric("Model calls", sum(span.kind == "llm" for span in steps))
    columns[2].metric("Policy decisions", sum(span.kind == "policy" for span in steps))
    columns[3].metric("Model cost", f"${cost:.4f}")
    components.trace_view(steps)
