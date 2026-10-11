"""Reusable UI pieces: badges, score bars, email and IO previews, the timeline, approval cards."""

from __future__ import annotations

import streamlit as st

from revenue_agents import story
from revenue_agents.crm import Activity
from revenue_agents.guardrails.approvals import ApprovalRequest
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Deal, LeadScore, Stage
from revenue_agents.stages import ORDER
from revenue_agents.system import System
from revenue_agents.tracking import ATTENTION_FLAGS
from revenue_agents.ui import context

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
    "reminder": ":material/alarm:",
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


def email_card(email: dict) -> None:
    st.markdown(f"**To:** {email['to']}  \n**Subject:** {email['subject']}")
    body = email["body"].strip().replace("<", "&lt;")
    st.markdown(f"<div class='ra-email'>{body}</div>", unsafe_allow_html=True)
    if email.get("lessons_used"):
        st.caption(f":material/psychology: Shaped by lessons: {', '.join(email['lessons_used'])}")


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


def approval_card(system: System, request: ApprovalRequest, key: str) -> None:
    """One high-risk action waiting for a human. Buttons send an approval event to the orchestrator."""
    action = request.action
    deal = system.crm.get_deal(action.deal_id)
    company = system.crm.get_lead(deal.lead_id).company
    with st.container(border=True):
        top = st.columns([5, 2])
        with top[0]:
            icon = ":material/mail:" if action.type is ActionType.SEND_EMAIL else ":material/description:"
            st.markdown(f"#### {icon} {action.summary}")
            st.markdown(f"<span class='ra-muted'>{company} · {deal.id} · proposed by the "
                        f"**{action.agent}** agent</span>", unsafe_allow_html=True)
        with top[1]:
            tier_badge(deal)
        st.warning(f"**Why a human must decide:** {request.reason}", icon=":material/policy:")

        if action.type is ActionType.SEND_EMAIL:
            email_card(action.payload)
        elif action.type is ActionType.SEND_IO:
            io_preview(action.payload["document"])

        is_email = action.type is ActionType.SEND_EMAIL
        note = st.text_input(
            "Feedback for a redraft" if is_email else "Note (optional)", key=f"{key}-note",
            placeholder="e.g. Mention their Q1 menu launch" if is_email else "e.g. checked the terms",
            help="If you reject an email with feedback, the outreach agent writes a new draft that follows it."
            if is_email else None)
        save_lesson = st.checkbox("Also save my feedback as a lesson for future emails", key=f"{key}-lesson",
                                  help="Only for feedback that applies to other leads too, not one-off fixes.")
        buttons = st.columns([3, 1.4, 1.4])
        reject_label = "Reject & redraft" if is_email and note.strip() else "Reject"
        if buttons[1].button(reject_label, key=f"{key}-reject", width="stretch"):
            _decide(system, request, False, note, save_lesson)
        if buttons[2].button("Approve", key=f"{key}-approve", type="primary", icon=":material/check:",
                             width="stretch"):
            _decide(system, request, True, note)


def _decide(system: System, request: ApprovalRequest, approved: bool, note: str, save_lesson: bool = False) -> None:
    reviewer = st.session_state.reviewer
    event = story.approval_event(request.id, approved, reviewer, note.strip() or None, save_lesson)
    try:
        lines = system.orchestrator.handle(event).steps
    except Exception as error:  # show it, never crash: the decision is recorded and can be retried with Run now
        st.error(f"{'Approval' if approved else 'Rejection'} could not complete: {type(error).__name__}: {error}")
        return
    context.log(f"{'Approved' if approved else 'Rejected'}: {request.action.summary}", lines)
    st.rerun()


def waiting_card(system: System, request: ApprovalRequest, key: str) -> None:
    """Approved by a human but not executed (kill switch, or a failure). Run now executes it once."""
    with st.container(border=True):
        st.markdown(f":material/pending_actions: **{request.action.summary}**")
        st.caption(f"Approved by {request.decided_by} · not executed yet"
                   + (" · kill switch is ON" if system.kill_switch.is_engaged() else ""))
        if st.button("Run now", key=f"{key}-run", icon=":material/play_arrow:",
                     disabled=system.kill_switch.is_engaged()):
            try:
                lines = system.orchestrator.run_approved(request.id)
            except Exception as error:
                st.error(f"Could not run: {type(error).__name__}: {error}")
                return
            context.log(f"Run now: {request.action.summary}", lines)
            st.rerun()


def segment_deals(system: System) -> list[Deal]:
    """Deals of the segment selected in the sidebar."""
    segment = st.session_state.segment
    return sorted((deal for deal in system.crm.deals() if deal.segment == segment), key=lambda deal: deal.id)


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
    if text.startswith(("Already handled", "Same webhook")):
        return f":material/content_copy: :gray[{text}]"
    if "→ executed" in text or text.startswith(("Approved", "Closed won")):
        return f":material/check_circle: :green[{text}]"
    return text


def log_entries(limit: int | None = None) -> None:
    """Newest event open; older ones collapsed. Digest text is shown as a block, keeping its layout."""
    for index, entry in enumerate(st.session_state.log[:limit]):
        container = st.container(border=True) if index == 0 else st.expander(entry.title)
        with container:
            if index == 0:
                st.markdown(f"**{entry.title}**")
            digest_at = next((i for i, line in enumerate(entry.lines) if line.startswith("Daily digest")), None)
            lines = entry.lines if digest_at is None else entry.lines[:digest_at]
            st.markdown("  \n".join(styled_line(line) for line in lines if line))
            if digest_at is not None:
                st.code("\n".join(entry.lines[digest_at:]), language=None)
