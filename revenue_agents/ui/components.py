"""Reusable UI pieces: badges, score bars, email and IO previews, the timeline, the agent trace."""

from __future__ import annotations

import streamlit as st

from revenue_agents.crm import Activity
from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Deal, LeadScore, Stage
from revenue_agents.stages import ORDER
from revenue_agents.system import System
from revenue_agents.team import LABELS
from revenue_agents.tracking import ATTENTION_FLAGS
from revenue_agents.ui import agent_views, context, feed, live
from revenue_agents.world import Desk

TIER_COLORS = {"A": "green", "B": "blue", "C": "gray"}
STAGE_LABELS = {
    Stage.PROSPECT: "Prospect", Stage.ENRICHED: "Enriched", Stage.SCORED: "Scored", Stage.OUTREACH: "Outreach",
    Stage.REPLIED: "Replied", Stage.MEETING: "Meeting", Stage.QUALIFIED: "Qualified", Stage.PROPOSAL: "Proposal",
    Stage.SIGNATURE: "Signature", Stage.HANDOFF: "Won · Handoff", Stage.CLOSED_LOST: "Closed lost",
}
ACTIVITY_ICONS = {
    "email_sent": ":material/mail:", "reply": ":material/reply:", "flag": ":material/flag:",
    "note": ":material/sticky_note_2:", "stage_change": ":material/trending_flat:", "document": ":material/description:",
    "io_sent": ":material/send:", "signed": ":material/draw:", "handoff": ":material/handshake:",
    "reminder": ":material/alarm:", "call_brief": ":material/assignment:", "call": ":material/call:",
    "io_voided": ":material/block:", "research": ":material/travel_explore:", "meeting_booked": ":material/event:",
    "follow_up": ":material/event_repeat:",
}

CSS = """
<style>
  .block-container { padding-top: 2.2rem; }
  [data-testid="stMetric"] { background: #F8F9FC; border: 1px solid #E5E7EB; border-radius: 0.6rem;
                             padding: 0.7rem 0.9rem; }
  [data-testid="stMetricLabel"] p { color: #6B7280; font-size: 0.82rem; }
  .ra-muted { color: #6B7280; font-size: 0.86rem; }
  .ra-column-title { font-weight: 650; font-size: 0.9rem; color: #374151; margin-bottom: 0.2rem; }
  .ra-email { background: #F8F9FC; border: 1px solid #E5E7EB; border-left: 3px solid #4F46E5;
              border-radius: 0.5rem; padding: 0.8rem 1rem; white-space: pre-wrap; font-size: 0.92rem; }
  /* Today */
  .ra-date { font-size: 0.85rem; color: #6B7280; }
  .ra-headline { font-size: 2.3rem; font-weight: 700; margin: 0.1rem 0 0.2rem; padding: 0; }
  .ra-lede { color: #4B5563; margin: 0; }
  .ra-status { font-size: 0.75rem; padding: 1px 8px; border-radius: 999px; background: #F3F4F6; color: #4B5563; }
  .ra-status.run { background: #DCFCE7; color: #166534; } .ra-status.busy { background: #DBEAFE; color: #1E40AF; }
  .ra-status.stop { background: #FEE2E2; color: #991B1B; }
  .ra-status.ask { background: #FFEDD5; color: #9A3412; }
  .st-key-ra-kill button { border-color: #DC2626; color: #DC2626; }
  .ra-need { display: grid; grid-template-columns: 0.9rem 1fr; gap: 0.5rem; align-items: baseline; }
  .ra-meta { font-family: ui-monospace, Menlo, monospace; font-size: 0.78rem; color: #6B7280; }
  .ra-need-title { font-weight: 650; font-size: 1.05rem; margin: 0.15rem 0; }
  .ra-need-why { color: #374151; font-size: 0.93rem; }
  .ra-rule { display: inline-block; margin-top: 0.4rem; font-size: 0.8rem; background: #F3F4F6; border-radius: 999px;
             padding: 1px 10px; }
  .ra-rule::first-letter { color: #DC2626; }
  .ra-feed-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 0.5rem; }
  .ra-feed-head h4 { margin: 0; }
  .ra-feed-head span { font-size: 0.8rem; color: #6B7280; }
  .ra-feed-head i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin: 0 4px 0 10px; }
  .ra-row { display: grid; grid-template-columns: 4.6rem 0.8rem 1fr; gap: 0.5rem; align-items: baseline;
            padding: 0.6rem 0; border-top: 1px solid #F1F2F5; font-size: 0.93rem; }
  .ra-row-text { color: #374151; margin-top: 0.15rem; }
  .ra-company { color: #4B5563; }
  .ra-outcome { font-size: 0.72rem; background: #F3F4F6; border-radius: 999px; padding: 1px 8px; margin-left: 4px; }
  .ra-chips { margin-top: 0.35rem; display: flex; flex-wrap: wrap; gap: 4px; }
  .ra-chip { font-family: ui-monospace, Menlo, monospace; font-size: 0.74rem; border: 1px solid #E5E7EB;
             border-radius: 4px; padding: 1px 6px; color: #4B5563; background: #FFFFFF; }
  .ra-chip.ok { color: #166534; border-color: #BBF7D0; } .ra-chip.fix { color: #92400E; border-color: #FDE68A; }
  .ra-chip.ask { color: #C2410C; border-color: #FDBA74; } .ra-chip.no { color: #B91C1C; border-color: #FECACA; }
  .st-key-ra-working { background: #17132A; border-radius: 0.8rem; padding: 1rem 1.1rem; }
  .st-key-ra-working h5, .st-key-ra-working p, .st-key-ra-working b { color: #F5F3FF; }
  .st-key-ra-working .ra-chip { background: transparent; color: #E9D5FF; border-color: #4C3F75; }
  .ra-calling { font-size: 0.82rem; opacity: 0.75; }
  .ra-tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
  .ra-tiles div { background: #F8F9FC; border-radius: 0.5rem; padding: 0.5rem 0.6rem; }
  .ra-tiles b { display: block; font-size: 1.4rem; } .ra-tiles span { font-size: 0.78rem; color: #6B7280; }
  /* Inbox */
  .ra-detail-title { margin: 0.2rem 0 0.6rem; padding: 0; }
  .ra-stopped { background: #FFF4ED; border-radius: 0.6rem; padding: 0.7rem 0.9rem; }
  .ra-stopped b { color: #9A3412; } .ra-stopped div { margin: 0.2rem 0; color: #374151; }
  .ra-stopped code { font-size: 0.78rem; background: transparent; color: #6B7280; padding: 0; }
  .ra-tag { float: right; font-family: ui-monospace, Menlo, monospace; font-size: 0.75rem; background: #F3F4F6;
            border-radius: 4px; padding: 1px 8px; }
  .st-key-ra-coach-inbox { background: #F5F3FF; border: 1px solid #DDD6FE; border-radius: 0.8rem; padding: 1rem; }
  .ra-tool { border-top: 1px solid #F1F2F5; padding: 0.45rem 0; }
  .ra-tool div { display: flex; justify-content: space-between; gap: 0.5rem; }
  .ra-tool code { background: transparent; padding: 0; color: #111827; }
  .ra-tool span { font-family: ui-monospace, Menlo, monospace; font-size: 0.78rem; color: #6B7280; }
  .ra-tool p { margin: 0.1rem 0 0; font-size: 0.82rem; color: #4B5563; }
  /* Ask */
  .ra-step-row { display: grid; grid-template-columns: 1.8rem 1fr; gap: 0.6rem; padding: 0.7rem 0;
                 border-top: 1px solid #F1F2F5; }
  .ra-num { width: 1.6rem; height: 1.6rem; border-radius: 50%; border: 1px solid #E5E7EB; display: flex;
            align-items: center; justify-content: center; font-size: 0.8rem; color: #374151; }
  .ra-step-top { display: flex; justify-content: space-between; gap: 0.5rem; }
  .ra-step-top span { font-family: ui-monospace, Menlo, monospace; font-size: 0.78rem; color: #6B7280; }
  .ra-step-row code { background: transparent; padding: 0; color: #6B7280; font-size: 0.8rem; }
  .ra-step-row p { margin: 0.15rem 0 0; color: #374151; font-size: 0.92rem; }
  .ra-step-row.no p { color: #B91C1C; }
  .ra-copilot-says { background: #F5F3FF; border-radius: 0.6rem; padding: 0.7rem 0.9rem; margin-top: 0.6rem; }
  /* Playbook */
  [class*="st-key-ra-coach"] { background: #F5F3FF; border: 1px solid #DDD6FE; border-radius: 0.8rem;
                                padding: 1rem 1.1rem; margin-bottom: 1rem; }
  /* Deal room */
  .ra-stages { display: grid; grid-template-columns: repeat(10, minmax(0, 1fr)); gap: 6px; margin: 0.4rem 0 1.2rem; }
  .ra-stages div { border-top: 4px solid #E5E7EB; padding-top: 6px; font-size: 0.78rem; color: #6B7280; }
  .ra-stages .done { border-color: #7C3AED; }
  .ra-stages .now { border-color: #DC2626; color: #111827; font-weight: 650; }
  .ra-day { font-size: 0.74rem; font-weight: 650; letter-spacing: 0.06em; text-transform: uppercase; color: #6B7280;
            margin: 1rem 0 0.2rem; padding-bottom: 0.3rem; border-bottom: 1px solid #E5E7EB; }
  .ra-beat { display: grid; grid-template-columns: 3.2rem 0.8rem 1fr; gap: 0.4rem; align-items: baseline;
             padding: 0.45rem 0; border-bottom: 1px solid #F1F2F5; font-size: 0.93rem; }
  .ra-time { font-family: ui-monospace, Menlo, monospace; font-size: 0.8rem; color: #6B7280; }
  .ra-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .ra-quote { background: #F8F9FC; border-radius: 0.4rem; padding: 0.4rem 0.7rem; margin-top: 0.35rem;
              color: #374151; }
  .st-key-ra-plan { background: #17132A; color: #F5F3FF; border-radius: 0.8rem; padding: 1.1rem 1.2rem; }
  .st-key-ra-plan h4, .st-key-ra-plan p, .st-key-ra-plan div { color: #F5F3FF; }
  .ra-step { display: grid; grid-template-columns: 1.2rem 1fr; gap: 0.3rem; padding: 0.3rem 0; font-size: 0.93rem; }
  .ra-step span { color: #A78BFA; }
  .ra-step.ra-you span { color: #F97316; }
  .ra-step.ra-you div { font-weight: 650; }
  .ra-plan-note { font-size: 0.8rem; opacity: 0.7; margin-top: 0.4rem; }
  .ra-field { padding: 0.45rem 0; border-bottom: 1px solid #F1F2F5; }
  .ra-field-top { display: flex; justify-content: space-between; gap: 0.8rem; font-size: 0.93rem; }
  .ra-field-top b { text-align: right; }
  @media (max-width: 700px) { .ra-stages { grid-template-columns: repeat(5, minmax(0, 1fr)); } }
</style>
"""


def inject_css() -> None:
    st.html(CSS)


def flash(message: str, icon: str) -> None:
    """A short confirmation that survives the rerun or page switch that follows a decision."""
    st.session_state.flash = (message, icon)


def show_flash() -> None:
    if flash := st.session_state.pop("flash", None):
        st.toast(flash[0].replace("$", "\\$"), icon=flash[1])


def page_header(title: str, subtitle: str) -> None:
    st.title(title)
    st.markdown(f"<p class='ra-muted'>{subtitle}</p>", unsafe_allow_html=True)


# --- Badges ------------------------------------------------------------------------------


def tier_badge(deal: Deal) -> None:
    if deal.score:
        st.badge(f"Tier {deal.score.tier.value} · {deal.score.total:g}", color=TIER_COLORS[deal.score.tier.value])
    else:
        st.badge("Not scored", color="gray")


def needs_attention(deal: Deal) -> bool:
    return any(flag in ATTENTION_FLAGS or flag.startswith("stalled_") for flag in deal.flags)


def flag_badges(flags: list[str]) -> None:
    if not flags:
        return
    with st.container(horizontal=True, gap="small"):
        for flag in flags:
            if flag == "possible_prompt_injection":
                st.badge("Prompt injection", icon=":material/gpp_bad:", color="red")
            elif flag in ATTENTION_FLAGS:
                st.badge(flag.replace("_", " "), icon=":material/priority_high:", color="orange")
            elif flag.startswith("stalled_"):
                st.badge(flag.replace("_", " "), icon=":material/hourglass_bottom:", color="yellow")
            else:
                st.badge(flag.replace("_", " "), color="blue")


def stage_bar(deal: Deal) -> None:
    """The stages as a row of bars, like the deal room mockup: done, where the deal is now, still to come."""
    if deal.stage is Stage.CLOSED_LOST:
        st.badge("Closed lost", color="gray")
        return
    position = ORDER.index(deal.stage)
    won = deal.stage is Stage.HANDOFF
    cells = "".join(f"<div class='{'done' if i < position or won else 'now' if i == position else ''}'>"
                    f"{STAGE_LABELS[stage]}</div>" for i, stage in enumerate(ORDER))
    st.markdown(f"<div class='ra-stages'>{cells}</div>", unsafe_allow_html=True)


def stage_progress(deal: Deal) -> None:
    if deal.stage is Stage.CLOSED_LOST:
        st.badge("Closed lost", color="gray")
        return
    position = ORDER.index(deal.stage) + 1
    st.progress(position / len(ORDER), text=f"Stage {position} of {len(ORDER)}: {STAGE_LABELS[deal.stage]}")


# --- Content blocks ---------------------------------------------------------------------


def score_breakdown(score: LeadScore) -> None:
    columns = st.columns(3)
    for column, name in zip(columns, ("fit", "intent", "engagement")):
        sub = getattr(score, name)
        with column, st.container(border=True):
            st.markdown(f"**{name.title()}** &nbsp; `{sub.value:g}/100`")
            st.progress(sub.value / 100)
            st.caption(sub.reason)


def email_card(system: System, email: dict) -> None:
    st.markdown(f"**To:** {email['to']}  \n**Subject:** {email['subject']}")
    body = email["body"].strip().replace("<", "&lt;")
    st.markdown(f"<div class='ra-email'>{body}</div>", unsafe_allow_html=True)
    lesson_notes(system, email.get("lessons_used") or [])


# --- Knowledge loop ------------------------------------------------------------------------------


def lesson_notes(system: System, lesson_ids: list[str]) -> None:
    """Which lessons shaped this, in words: what each says and where it was learned."""
    if not lesson_ids:
        return
    known = {lesson.id: lesson for lesson in system.lessons.all()}
    lines = []
    for lesson_id in lesson_ids:
        lesson = known.get(lesson_id)
        if lesson is None:
            lines.append(f"- {lesson_id}")
            continue
        lines.append(f"- “{_plain(lesson.text)}” <span class='ra-muted'>· learned from {learned_from(lesson)}</span>")
    st.markdown(":material/psychology: **Shaped by what the system learned:**  \n" + "  \n".join(lines),
                unsafe_allow_html=True)


def learned_from(lesson) -> str:
    kind = {"call": "call", "reply": "reply", "review": "a reviewer's note on"}.get(lesson.source, lesson.source)
    return f"{kind} {lesson.source_id}"


def used_in(system: System, use) -> str:
    """A lesson use, in words: "email to Harbor Lane Home", "call brief for BrightBite Burgers"."""
    try:
        company = system.crm.get_lead(system.crm.get_deal(use.deal_id).lead_id).company
    except KeyError:
        company = use.deal_id
    kind = use.used_in.split(":")[0]
    if kind == "send_email":
        return f"answer email to {company}" if ":reply:" in use.used_in else f"email to {company}"
    if kind == "prepare_call_brief":
        return f"call brief for {company}"
    return f"{kind.replace('_', ' ')} for {company}"


def _plain(text: str) -> str:
    """Model-written text shown with HTML on: no markup gets through."""
    return text.replace("<", "&lt;")


def io_preview(document: str) -> None:
    keys = ("| Signatory", "**Flight dates:**", "**Total budget:**", "**Payment terms:**")
    st.markdown("  \n".join(line.lstrip("- ") for line in document.splitlines() if any(k in line for k in keys)))
    with st.expander("Full insertion order"):
        st.markdown(document)


def timeline(activities: list[Activity]) -> None:
    for activity in reversed(activities):
        icon = ACTIVITY_ICONS.get(activity.kind, ":material/circle:")
        st.markdown(f"{icon} **{activity.summary}**  \n"
                    f"<span class='ra-muted'>{activity.at:%b %d, %H:%M} · {activity.kind.replace('_', ' ')}</span>",
                    unsafe_allow_html=True)


# --- Approvals ----------------------------------------------------------------------------


def editable_email(email: dict, key: str) -> dict[str, str]:
    """The draft as editable fields. Returns only what the reviewer changed."""
    st.markdown(f"**To:** {email['to']}")
    subject = st.text_input("Subject", value=email["subject"], key=f"{key}-subject")
    body = st.text_area("Email", value=email["body"].strip(), key=f"{key}-body", height=280,
                        help="Edit the wording if you like. Approving sends your version; the agent's draft is kept.")
    edits = {field: value for field, value in (("subject", subject), ("body", body))
             if value.strip() != email[field].strip()}
    if edits:
        st.info("You edited this email. Approving sends your version, and the agent's original draft is kept for "
                "the record.", icon=":material/edit:")
    return edits


def segment_deals(system: System) -> list[Deal]:
    """Deals of the segment selected in the sidebar (all of them by default)."""
    return sorted((deal for deal in system.crm.deals() if context.in_segment(deal)), key=lambda deal: deal.id)


def styled_line(line: str) -> str:
    """One orchestrator step as a coloured, icon-led line, so the log can be scanned at a glance."""
    text = line.replace("->", "→").replace("[", "(").replace("]", ")").replace("$", "\\$")
    if "→ executed" in text and text.startswith("Stage "):
        return f":material/trending_flat: :violet[{text}]"
    if text.startswith("Stage "):
        return f":material/trending_flat: :violet[**{text}**]"
    if "→ queued" in text or text.startswith(("Pricing question", "Not qualified")):
        return f":material/front_hand: :orange[**{text}**]"
    if "→ blocked" in text or text.startswith(("Rejected", "Stopped", "Suspicious")):
        return f":material/block: :red[{text}]"
    if text.startswith(("Flags:", "Flag ")) or "stalled" in text:
        return f":material/flag: :orange[{text}]"
    if text.startswith(("Lesson", "Draft uses lessons")):
        return f":material/psychology: :blue[{text}]"
    if text.startswith("Call brief"):
        return f":material/assignment: :blue[**{text}**] (see Deal room → Call brief)"
    if text.startswith(("Already handled", "Same webhook")):
        return f":material/content_copy: :gray[{text}]"
    if "→ executed" in text or text.startswith(("Approved", "Closed won")):
        return f":material/check_circle: :green[{text}]"
    return text


# --- Agent trace ------------------------------------------------------------------------------

DECISION_COLORS = {"auto": "green", "needs_approval": "orange", "blocked": "red"}


def trace_view(spans: list) -> None:
    """One event, step by step: each agent run with the tools it chose, the rule behind each call and what the
    gateway did, in order; then each code skill, and every decision, execution and stage change outside a run."""
    runs = iter(feed.runs_in(spans))
    inside: list | None = None
    for span in spans:
        if span.kind == "run":
            inside = []
        elif span.kind == "agent" and "tool_calls" in span.data:
            _agent_run(next(runs), span, inside or [])
            inside = None
        elif inside is not None:
            inside.append(span)
        else:
            _step(span)


def _step(span) -> None:
    if span.kind == "agent":
        _skill_step(span)
    elif span.kind == "tool":
        agent_views.run_details(feed.Run(span.data.get("agent", ""), "", "", span.at, "", "", "", None, 0,
                                         [_tool_step(span)]), footer=False)
    elif span.kind == "policy":
        decision = span.data["decision"].split(" ")[0]
        st.markdown(f":material/policy: **Policy** · `{span.name}` → "
                    f":{DECISION_COLORS.get(decision, 'gray')}[**{span.data['decision']}**] "
                    f"<span class='ra-muted'>[{span.data['rule']}] {span.data['reason']}</span>",
                    unsafe_allow_html=True)
    elif span.kind == "action":
        st.markdown(f":material/check_circle: :green[Executed `{span.name}`] "
                    f"<span class='ra-muted'>({span.data['action_id']})</span>", unsafe_allow_html=True)
    elif span.kind == "stage":
        moved = span.data["from"] != span.data["to"]
        text = f"Stage {span.data['from']} → {span.data['to']}" if moved else f"Stage stays {span.data['from']}"
        st.markdown(f":material/trending_flat: :violet[**{text}**] <span class='ra-muted'>({span.data['why']})</span>",
                    unsafe_allow_html=True)
    elif span.kind == "llm":
        st.markdown(f":material/record_voice_over: **Buyer simulator** wrote a reply · {span.data.get('model')} "
                    f"<span class='ra-muted'>· {span.duration_ms} ms</span>", unsafe_allow_html=True)


def _agent_run(run, span, steps: list) -> None:
    with st.container(border=True):
        st.markdown(f":material/smart_toy: **{run.label}** · {run.title} "
                    f"<span class='ra-muted'>· {run.model} · {len(run.tools)} tool calls · {run.ms / 1000:.1f} s</span>",
                    unsafe_allow_html=True)
        st.markdown(agent_views.chips(run.tools))
        if run.error:
            st.error(run.error)
        for step in steps:
            _step(step)
        if run.summary:
            st.caption(f"The agent: “{_escape(run.summary)}”")
        with st.expander("The agent's instructions and task (what the model was given)"):
            st.code(f"SYSTEM:\n{span.data.get('system', '')}\n\nTASK:\n{span.data.get('prompt', '')}", language=None,
                    wrap_lines=True)


def _tool_step(span) -> feed.ToolStep:
    data = span.data
    return feed.ToolStep(span.name, data.get("verdict", ""), data.get("outcome"), data.get("rule", ""),
                         data.get("reason", ""), data.get("result", ""), data.get("is_error", False))


def _skill_step(span) -> None:
    with st.container(border=True):
        st.markdown(f":material/functions: **{LABELS.get(span.name, span.name)}** · plain code "
                    f"<span class='ra-muted'>· {span.duration_ms} ms</span>", unsafe_allow_html=True)
        if "error" in span.data:
            st.error(span.data["error"])
            return
        proposed = span.data.get("proposed", [])
        st.markdown(":material/outgoing_mail: **Proposed:** " + (", ".join(f"`{p}`" for p in proposed) or "nothing"))


# --- Conversation ------------------------------------------------------------------------------


def conversation(system: System, deal: Deal) -> None:
    """The deal's email thread, both ways, like an email app, with the agents' notes on each reply."""
    lead = system.crm.get_lead(deal.lead_id)
    messages = sorted(system.crm.messages(deal.id), key=lambda message: message.at)
    drafts = [r for r in system.approvals.pending() if r.action.deal_id == deal.id
              and r.action.type is ActionType.SEND_EMAIL]
    if not messages and not drafts:
        st.caption("No emails yet. The first email appears here once it is sent (Tier A waits for approval first).")
        return

    for message in messages:
        outbound = message.direction == "outbound"
        who = "Daniel Reyes · Cool AI" if outbound else f"{lead.contact.name} · {lead.company}"
        with st.chat_message("assistant" if outbound else "user",
                             avatar=":material/send:" if outbound else ":material/person:"):
            st.markdown(f"**{who}** <span class='ra-muted'>· {message.at:%b %d, %H:%M} · "
                        f"{'sent' if outbound else 'received'}</span>", unsafe_allow_html=True)
            st.markdown(f"**Subject:** {_escape(message.subject)}")
            body = _escape(message.body.strip().replace("<", "&lt;"))
            st.markdown(f"<div class='ra-email'>{body}</div>", unsafe_allow_html=True)
            if outbound:
                request = next((r for r in system.approvals.all() if r.id == message.meta.get("action_id")), None)
                note = (f"approved by {request.decided_by}" if request and request.status is ApprovalStatus.APPROVED
                        else "sent automatically (policy: low risk)")
                if message.meta.get("edited_by"):
                    note += f" · edited by {message.meta['edited_by']} before sending"
                st.caption(f":material/verified_user: {note}")
                lesson_notes(system, message.meta.get("lessons_used") or [])
                if request and request.original_action:
                    with st.expander("Show the agent's original draft"):
                        original = request.original_action.payload
                        st.markdown(f"**Subject:** {_escape(original['subject'])}")
                        st.markdown(f"<div class='ra-email'>{_escape(original['body'].strip().replace('<', '&lt;'))}"
                                    f"</div>", unsafe_allow_html=True)
            else:
                meta = message.meta
                linked = f"answers {message.in_reply_to}" if message.in_reply_to else "not linked to any email we sent"
                st.caption(f":material/smart_toy: Reply agent: **{meta.get('intent')}** · "
                           f"flags: {', '.join(meta.get('flags', [])) or 'none'} · {linked}")
                if meta.get("suggested_next_step"):
                    st.caption(f":material/lightbulb: Next step: {meta['suggested_next_step']}")

    for request in drafts:
        with st.chat_message("assistant", avatar=":material/edit_note:"):
            st.markdown("**Draft waiting for approval** <span class='ra-muted'>· not sent</span>",
                        unsafe_allow_html=True)
            email_card(system, request.action.payload)


def _escape(text: str) -> str:
    """Streamlit treats $...$ as a maths formula; show dollar signs as text."""
    return text.replace("$", "\\$")



# --- Contract terms ---------------------------------------------------------------------------


def terms_form(desk: Desk, deal: Deal, key: str) -> None:
    """The deal's commercial terms. Saving a change updates the deal and the deal desk redrafts the IO from it."""
    fields = deal.fields
    with st.form(key, border=False):
        left, right = st.columns(2)
        budget = left.number_input("Budget (USD)", min_value=0, step=5000, value=fields.budget_usd or 0)
        payment = right.text_input("Payment terms", value=fields.payment_terms or "")
        start = left.date_input("Flight start", value=fields.flight_start)
        end = right.date_input("Flight end", value=fields.flight_end)
        term = left.text_input("Contract term", value=fields.contract_term or "")
        scope = right.text_input("Scope", value=fields.scope or "")
        special = st.text_area("Special terms", value=fields.special_terms or "", height=80)
        submitted = st.form_submit_button("Save terms and redraft the IO", icon=":material/autorenew:")
    if not submitted:
        return
    new = {"budget_usd": int(budget), "payment_terms": payment.strip(), "flight_start": start, "flight_end": end,
           "contract_term": term.strip(), "scope": scope.strip(), "special_terms": special.strip()}
    changes = {name: (value.isoformat() if hasattr(value, "isoformat") else value) for name, value in new.items()
               if value and value != getattr(fields, name)}
    if not changes:
        st.info("Nothing changed.", icon=":material/info:")
        return
    with st.spinner("The deal desk is redrafting the IO…"):
        live.change_terms(desk.world, deal.id, changes, context.reviewer())
    failed = [d for d in desk.world.log() if d.source == "you" and d.status == "failed"]
    if failed and failed[-1].label.endswith(deal.id):  # e.g. an invalid value: nothing was changed
        st.error(f"Terms not saved: {failed[-1].steps[0]}", icon=":material/error:")
        return
    st.rerun()
