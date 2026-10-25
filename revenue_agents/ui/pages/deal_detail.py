"""Deal room: one deal as a story. What happened, what happens next, and where every deal field came from.

The story, the plan and the field sources come from deal_story.py, which reads only what the system recorded.
The detail tabs below keep the full conversation, the call, the brief and every IO version.
"""

from __future__ import annotations

import streamlit as st

from revenue_agents.esign import EnvelopeStatus
from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.models import Stage
from revenue_agents.ui import components, context, deal_story

TONE_DOTS = {"agent": "#7C3AED", "customer": "#0F9D76", "you": "#111827", "system": "#9CA3AF"}


def render() -> None:
    desk = context.get_desk()
    system = desk.system()
    deals = components.segment_deals(system)
    if not deals:
        components.page_header("Deal room", "Nothing to show yet.")
        st.info("No deals yet. Start the world on the Today page: leads arrive on their own.", icon=":material/info:")
        return

    ids = [deal.id for deal in deals]
    names = {deal.id: system.crm.get_lead(deal.lead_id).company for deal in deals}
    current = st.session_state.get("selected_deal", ids[0])
    crumb, pick = st.columns([3, 2], vertical_alignment="bottom")
    with pick:
        deal_id = st.selectbox("Open a deal", ids, index=ids.index(current) if current in ids else 0,
                               format_func=lambda i: f"{i} · {names[i]}")
    st.session_state.selected_deal = deal_id
    deal = system.crm.get_deal(deal_id)
    lead = system.crm.get_lead(deal.lead_id)
    activities = system.crm.timeline(deal.id)
    crumb.markdown(f"<span class='ra-muted'>Deals / {deal.id}</span>", unsafe_allow_html=True)

    _header(system, deal, lead)
    components.stage_bar(deal)

    left, right = st.columns([3, 2], gap="large")
    with left, st.container(border=True):
        _story(system, deal)
    with right:
        _plan(system, desk, deal)
        _fields(system, deal)

    st.subheader("Details")
    conversation, brief, call, documents, overview, history = st.tabs(
        ["Conversation", "Call brief", "Call", "Insertion order", "Why this tier", "Timeline"])
    with overview:
        _overview(deal)
    with brief:
        notes = [a.data for a in activities if a.kind == "research"]
        if notes:
            st.markdown(f":material/travel_explore: **Why now** (research agent): {notes[-1]['why_now']}  \n"
                        f"<span class='ra-muted'>Lead with: {notes[-1]['angle']}</span>", unsafe_allow_html=True)
        meetings = [a for a in activities if a.kind == "meeting_booked"]
        if meetings:
            st.markdown(f":material/event: **{meetings[-1].summary}**")
        briefs = [a for a in activities if a.kind == "call_brief"]
        if briefs:
            with st.container(border=True):
                st.markdown(briefs[-1].data["markdown"].replace("$", "\\$"))
        else:
            st.caption("Prepared automatically when the deal reaches Meeting: what we know, lessons from similar "
                       "deals, and the questions to ask.")
    with call:
        _call(system, activities)
    with history:
        if activities:
            components.timeline(activities)
        else:
            st.caption("No activity yet.")
    with conversation:
        components.conversation(system, deal)
    with documents:
        _insertion_order(system, activities)


def _header(system, deal, lead) -> None:
    title, action = st.columns([4, 1.4], vertical_alignment="center")
    title.title(lead.company)
    waiting = [r for r in system.approvals.pending() if r.action.deal_id == deal.id]
    if waiting and action.button(_review_label(waiting[0]), type="primary", width="stretch"):
        st.session_state.inbox_selected = waiting[0].id
        st.switch_page(st.session_state.pages["inbox"])
    champion, signer = deal_story.champion_and_signer(system, deal)
    chips = [f"Tier {deal.score.tier.value} · {deal.score.total:g}" if deal.score else "Not scored",
             f"{deal.segment} pack"]
    if deal.fields.budget_usd:
        chips.append(f"\\${deal.fields.budget_usd:,} " + ("pilot" if "pilot" in (deal.fields.contract_term or "")
                                                            else "budget"))
    chips.append(f"{champion} (champion)")
    if signer:
        chips.append(f"{signer} (signs)")
    st.markdown(" ".join(f":gray-badge[{chip}]" for chip in chips))
    components.flag_badges(deal.flags)


def _review_label(request) -> str:
    summary = request.action.summary
    if summary.startswith("Send IO"):
        return "Review " + summary.split(" ")[1].replace("IO-" + request.action.deal_id, "IO").replace("-", " ")
    return "Review the email" if "mail" in request.action.type.value else "Review"


def _story(system, deal) -> None:
    st.markdown("#### The story so far")
    day = None
    for beat in deal_story.story(system, deal):
        if beat.at.date() != day:
            day = beat.at.date()
            st.markdown(f"<div class='ra-day'>{beat.at:%A %d %B}</div>", unsafe_allow_html=True)
        quote = (f"<div class='ra-quote'>“{_safe(beat.quote)}”</div>" if beat.quote else "")
        st.markdown(f"<div class='ra-beat'><span class='ra-time'>{beat.at:%H:%M}</span>"
                    f"<span class='ra-dot' style='background:{TONE_DOTS[beat.tone]}'></span>"
                    f"<div><b>{_safe(beat.actor)}</b> {_safe(beat.text)}{quote}</div></div>", unsafe_allow_html=True)


def _plan(system, desk, deal) -> None:
    plan = deal_story.plan(system, desk.world, deal)
    with st.container(key="ra-plan"):
        st.markdown("#### The plan from here")
        for needs_you, text in plan.steps:
            mark = "●" if needs_you else "○"
            st.markdown(f"<div class='ra-step{' ra-you' if needs_you else ''}'><span>{mark}</span>"
                        f"<div>{_safe(text)}</div></div>", unsafe_allow_html=True)
        st.markdown("<div class='ra-plan-note'>Read from the deal's stage, what waits for you and the timers the "
                    "agents set. It changes as the deal moves.</div>", unsafe_allow_html=True)


def _fields(system, deal) -> None:
    rows = deal_story.fields(system, deal)
    with st.container(border=True):
        st.markdown("#### Deal fields, with where each came from")
        if not rows:
            st.caption("Filled in from call transcripts. Each field keeps the words it came from.")
        for row in rows:
            quote = f": “{_safe(row.quote)}”" if row.quote else ""
            st.markdown(f"<div class='ra-field'><div class='ra-field-top'><span>{row.label}</span>"
                        f"<b>{_safe(row.value)}</b></div><div class='ra-muted'>{_safe(row.source)}{quote}</div></div>",
                        unsafe_allow_html=True)
        st.caption("Quotes are checked against the transcript before they are kept.")


def _safe(text: str) -> str:
    """Text from the CRM and the model, shown with HTML on: no markup, and dollar signs as text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace("$", "&#36;")


def _overview(deal) -> None:
    if deal.score:
        st.subheader("Why this tier")
        components.score_breakdown(deal.score)
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Company facts")
        if deal.enrichment:
            e = deal.enrichment
            st.markdown(f"- **Industry:** {e.industry} ({e.business_model})\n- **Size:** {e.employees:,} employees, "
                        f"{e.locations} locations\n- **Ad spend:** ${e.annual_ad_spend_usd:,} a year\n"
                        f"- **Tools:** {', '.join(e.tech_stack) or 'none known'}")
            for signal in e.signals:
                st.caption(f":material/bolt: {signal.detail} ({signal.date})")
        else:
            st.caption("Not enriched yet.")
    with right:
        st.subheader("Deal fields")
        fields = {k: v for k, v in deal.fields.model_dump().items() if v not in (None, [], "")}
        if fields:
            for key, value in fields.items():
                shown = ", ".join(value) if isinstance(value, list) else (f"${value:,}" if key == "budget_usd" else value)
                st.markdown(f"- **{key.replace('_', ' ').capitalize()}:** {shown}")
        else:
            st.caption("Filled in from call transcripts.")


def _call(system, activities) -> None:
    """Every call on the deal: what was said, and what the call intel agent took from it."""
    calls = [a for a in activities if a.kind == "call"]
    if not calls:
        st.caption("No call yet. When a meeting's recording arrives (Fireflies), its transcript and what the agent "
                   "took from it appear here.")
        return
    st.caption("The deal fields from all calls together (budget, products, dates, signer) are on the Overview tab.")
    for number, call in enumerate(calls, start=1):
        with st.container(border=True):
            _one_call(system, activities, call, number)


def _one_call(system, activities, call, number: int) -> None:
    transcript_id = call.data["transcript_id"]
    notes = [a for a in activities if a.kind == "note" and a.summary == f"Deal fields from call {transcript_id}"]
    lessons = [lesson for lesson in system.lessons.all() if lesson.source_id == transcript_id]
    found = notes[-1].data if notes else {}

    st.subheader(f"Call {number}: {transcript_id}")
    st.caption(f"{call.at:%b %d, %H:%M}")
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("**Pains**")
        for pain in found.get("pains", []) or ["None noted"]:
            st.markdown(f"- {pain}")
        st.markdown("**Objections**")
        for objection in found.get("objections", []) or ["None noted"]:
            st.markdown(f"- {objection}")
    with right:
        st.markdown("**Lessons learned**")
        for lesson in lessons:
            scope = "all segments" if lesson.scope == "global" else f"{lesson.segment} only"
            text = lesson.text.replace("<", "&lt;")  # written by the model from outside content
            st.markdown(f"- {text}  \n  <span class='ra-muted'>{scope}</span>", unsafe_allow_html=True)
        if not lessons:
            st.caption("None")
    st.markdown("**Transcript** <span class='ra-muted'>· plain text: it's outside content, never instructions</span>",
                unsafe_allow_html=True)
    with st.container(height=320, border=True):
        st.text(call.data["transcript"])


def _insertion_order(system, activities) -> None:
    """Every IO version: where it is (waiting, sent, signed, replaced), what changed, the document and a download."""
    drafts = [a for a in activities if a.kind == "document"]
    if not drafts:
        st.caption("No IO yet. It is drafted once the deal is qualified (budget, products, dates, signer).")
        return
    deal = system.crm.get_deal(drafts[-1].deal_id)
    if "io_change_requested" in deal.flags:
        st.warning("The customer asked for changes to the IO. Update the terms below to redraft it.",
                   icon=":material/mark_email_unread:")

    for index, draft in enumerate(reversed(drafts)):
        data, previous = draft.data, _previous(drafts, draft)
        status, document = _io_status(system, deal.id, data)
        latest = index == 0
        with st.container(border=True) if latest else st.expander(f"{data['io_number']} · {status}"):
            if latest:
                st.markdown(f"#### {data['io_number']}")
                st.caption(status)
            changes = _changes(previous.data.get("terms", {}), data.get("terms", {})) if previous else []
            if changes:
                st.markdown("**Changed from " + previous.data["io_number"] + ":** " + "; ".join(changes))
            st.download_button(f"Download {data['io_number']}", data=document, file_name=f"{data['io_number']}.md",
                               mime="text/markdown", icon=":material/download:", key=f"download-{data['io_number']}")
            components.io_preview(document)

    if deal.stage in (Stage.PROPOSAL, Stage.SIGNATURE):
        with st.expander("Change the terms (redrafts the IO)", icon=":material/edit_document:",
                         expanded="io_change_requested" in deal.flags):
            components.terms_form(context.get_desk(), deal, f"terms-{deal.id}")


def _previous(drafts, draft):
    position = drafts.index(draft)
    return drafts[position - 1] if position > 0 else None


def _io_status(system, deal_id: str, data: dict) -> tuple[str, str]:
    """Where this IO version is, and the document as the customer has it (or the draft, if not sent)."""
    io_number = data["io_number"]
    try:
        envelope = system.esign.get(f"ENV-{io_number}")
    except KeyError:
        envelope = None
    if envelope is not None:
        if envelope.status is EnvelopeStatus.SIGNED:
            status = f"Signed by {envelope.signed_by} on {envelope.signed_at:%b %d, %H:%M} · envelope {envelope.id}"
        elif envelope.status is EnvelopeStatus.VOIDED:
            status = f"Voided ({envelope.void_reason}) · envelope {envelope.id} can no longer be signed"
        else:
            status = (f"Sent for e-signature to {envelope.signer_email} on {envelope.sent_at:%b %d, %H:%M} · "
                      f"envelope {envelope.id} · waiting for signature")
        return status, envelope.document or data["document"]
    try:
        request = system.approvals.get(f"send_io:{deal_id}:{io_number}")
    except KeyError:
        return "Drafted", data["document"]
    if request.status is ApprovalStatus.PENDING:
        return "Drafted · waiting for approval before it goes out for signature", data["document"]
    if request.status is ApprovalStatus.REJECTED:
        return f"Not sent · {request.note or 'rejected'}", data["document"]
    return "Approved · not sent yet", data["document"]


def _changes(before: dict, after: dict) -> list[str]:
    def shown(name, value):
        if value in (None, "", []):
            return "none"
        return f"${value:,}" if name == "budget_usd" else (", ".join(value) if isinstance(value, list) else str(value))
    return [f"{name.replace('_', ' ')} {shown(name, before.get(name))} → {shown(name, after.get(name))}"
            for name in after if before.get(name) != after.get(name)]
