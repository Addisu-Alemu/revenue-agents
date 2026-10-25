"""Ask: give the copilot a goal in plain words and watch it work across deals, step by step.

The copilot uses the same tools, hook, policy and gateway as every other agent. It can read everything and change
only a follow-up timer or a note for the rep; the page shows every tool call it made, from the trace.
"""

from __future__ import annotations

import streamlit as st

from revenue_agents.ui import components, context, feed, live

EXAMPLES = ("Which deals went quiet after our first email? Follow up",
            "Follow up with every deal that went quiet after a pricing question",
            "Follow up with agency deals that went quiet")
STEP_TITLES = {"search_deals": "Find the deals", "read_deal": "Read the deal", "search_lessons":
               "Look for what has worked before", "set_follow_up": "Set a follow-up", "flag_for_rep":
               "Leave a note for the rep"}


def render() -> None:
    desk = context.get_desk()
    system = desk.system()
    components.page_header("Ask the team", "Give a goal in plain words. The copilot plans across deals, uses the same "
                                           "tools and the same rules as every other agent, and shows its work. It "
                                           "can read everything and change only a follow-up timer or a note for the "
                                           "rep; anything else it leaves to you.")
    if not desk.world.exists():
        st.info("Start the world on the Today page first: the copilot works on the deals in it.", icon=":material/info:")
        return

    with st.container(border=True):
        goal, run = st.columns([5, 1], vertical_alignment="bottom")
        text = goal.text_input("Goal", key="ask_goal", placeholder=EXAMPLES[0], label_visibility="collapsed")
        if run.button("Run", type="primary", width="stretch", disabled=not text.strip()):
            with st.spinner("The copilot is working…"):
                live.ask(desk.world, text.strip(), context.reviewer())
            st.rerun()
        with st.container(horizontal=True, gap="small"):
            for example in EXAMPLES:
                st.button(example, key=f"example-{example}", type="tertiary", on_click=_use, args=(example,))

    asks = [entry for entry in reversed(desk.world.log()) if entry.label.startswith("You asked:")]
    if not asks:
        st.caption("Nothing asked yet.")
        return
    spans = system.tracer.spans()
    _show(asks[0], spans, latest=True)
    if len(asks) > 1:
        with st.expander(f"Earlier goals · {len(asks) - 1}", icon=":material/history:"):
            for entry in asks[1:10]:
                _show(entry, spans, latest=False)


def _use(example: str) -> None:
    st.session_state.ask_goal = example


def _show(entry, spans, latest: bool) -> None:
    runs = [run for event_id in entry.events for run in feed.runs_in([s for s in spans if s.event_id == event_id])
            if run.agent == "copilot"]
    goal = entry.label.removeprefix("You asked: ")
    if not runs:
        failed = entry.steps[0] if entry.steps else "nothing was recorded"
        st.warning(f"“{goal}”: the copilot didn't run ({failed}).", icon=":material/error:")
        return
    run = runs[-1]
    with st.container(border=True):
        total = sum(step.ms for step in run.tools) / 1000
        st.markdown(f"<div class='ra-feed-head'><h4>{'What the copilot did' if latest else _html(goal)}</h4>"
                    f"<span>{len(run.tools)} steps · {total:.1f} s · {_html(run.model)}</span></div>"
                    + (f"<div class='ra-muted'>Goal: {_html(goal)} · {entry.at:%a %d %b %H:%M}</div>" if latest else ""),
                    unsafe_allow_html=True)
        rows = "".join(_step(number, step) for number, step in enumerate(run.tools, start=1))
        st.markdown(rows, unsafe_allow_html=True)
        if run.summary:
            st.markdown(f"<div class='ra-copilot-says'><b>The copilot:</b> {_html(run.summary)}</div>",
                        unsafe_allow_html=True)
        st.caption("Every call passed the same PreToolUse hook and policy as every other agent's; changes went "
                   "through the gateway and are in the audit log.")


def _step(number: int, step: feed.ToolStep) -> str:
    title = STEP_TITLES.get(step.tool, step.tool)
    if step.tool == "read_deal" and step.input.get("deal_id"):
        title = f"Read {step.input['deal_id']}"
    args = ", ".join(f"{key}={value}" for key, value in step.input.items() if value not in (None, ""))
    refused = step.tone == "refused"
    result = f"Refused: {step.reason}" if refused and step.reason else _first_line(step.result)
    return (f"<div class='ra-step-row{' no' if refused else ''}'><span class='ra-num'>{number}</span><div>"
            f"<div class='ra-step-top'><b>{_html(title)}</b><span>{step.ms / 1000:.1f} s</span></div>"
            f"<code>{_html(step.tool)}({_html(args)})</code><p>{_html(result)}</p></div></div>")


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    shown = lines[0] if len(lines) == 1 else f"{lines[0]} {'; '.join(line.lstrip('- ') for line in lines[1:4])}"
    return shown if len(shown) <= 220 else shown[:217].rstrip() + "…"


def _html(text: str) -> str:
    """Text shown inside HTML: no markup, dollar signs as text."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace("$", "&#36;")
