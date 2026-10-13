"""Deal detail: why it scored as it did, what we know, everything that happened, and its documents."""

from __future__ import annotations

import streamlit as st

from revenue_agents.esign import EnvelopeStatus
from revenue_agents.guardrails.approvals import ApprovalStatus
from revenue_agents.models import Stage
from revenue_agents.ui import components, context


def render() -> None:
    system = context.get_system()
    deals = components.segment_deals(system)
    if not deals:
        components.page_header("Deal", "Nothing to show yet.")
        st.info("No deals yet. Start the story on the Live demo page.", icon=":material/info:")
        return

    ids = [deal.id for deal in deals]
    names = {deal.id: system.crm.get_lead(deal.lead_id).company for deal in deals}
    current = st.session_state.get("selected_deal", ids[0])
    deal_id = st.selectbox("Deal", ids, index=ids.index(current) if current in ids else 0,
                           format_func=lambda i: f"{i} · {names[i]}")
    st.session_state.selected_deal = deal_id
    deal = system.crm.get_deal(deal_id)
    lead = system.crm.get_lead(deal.lead_id)
    activities = system.crm.timeline(deal.id)

    components.page_header(lead.company, f"{lead.contact.name}, {lead.contact.title} · {lead.contact.email} · "
                                         f"segment: {lead.segment} · source: {lead.source.replace('_', ' ')}")
    with st.container(horizontal=True, vertical_alignment="center"):
        components.tier_badge(deal)
        components.flag_badges(deal.flags)
    components.stage_progress(deal)

    overview, conversation, brief, call, history, documents = st.tabs(
        ["Overview", "Conversation", "Call brief", "Call", "Timeline", "Insertion order"])
    with overview:
        _overview(deal)
    with brief:
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
            components.terms_form(system, deal, f"terms-{deal.id}")


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
