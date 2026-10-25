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
</style>
"""


def inject_css() -> None:
    st.html(CSS)


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


def knowledge_loop(system: System) -> None:
    """Every lesson: what it says, where it was learned, who it applies to, and where it has been used."""
    segment = st.session_state.segment
    lessons = sorted((lesson for lesson in system.lessons.all() if segment == "all" or lesson.applies_in(segment)),
                     key=lambda item: item.created_at)
    uses = {lesson.id: list(dict.fromkeys(used_in(system, use) for use in system.lessons.uses(lesson.id)))
            for lesson in lessons}
    used = sum(bool(places) for places in uses.values())
    with st.expander(f"Knowledge loop: {len(lessons)} learned · {used} used", icon=":material/psychology:"):
        if not lessons:
            st.caption("Nothing learned yet. Lessons come from replies, call transcripts and reviewers' notes.")
            return
        for lesson in lessons:
            scope = "all segments" if lesson.scope == "global" else f"{lesson.segment} only"
            places = uses[lesson.id]
            st.markdown(f"**“{_escape(_plain(lesson.text))}”**  \n"
                        f"<span class='ra-muted'>Learned from {learned_from(lesson)} · {scope}</span>  \n"
                        + (f":material/arrow_forward: Used in: {', '.join(places)}" if places
                           else "<span class='ra-muted'>Not used yet</span>"),
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
        return f":material/assignment: :blue[**{text}**] (see Deal detail → Call brief)"
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
