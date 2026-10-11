"""Deal detail: why it scored as it did, what we know, everything that happened, and its documents."""

from __future__ import annotations

import streamlit as st

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

    overview, conversation, brief, history, documents = st.tabs(
        ["Overview", "Conversation", "Call brief", "Timeline", "Insertion order"])
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
    with history:
        if activities:
            components.timeline(activities)
        else:
            st.caption("No activity yet.")
    with conversation:
        components.conversation(system, deal)
    with documents:
        docs = [a for a in activities if a.kind == "document"]
        if docs:
            components.io_preview(docs[-1].data["document"])
            signed = [a for a in activities if a.kind == "signed"]
            if signed:
                st.success(signed[-1].summary, icon=":material/draw:")
        else:
            st.caption("No IO yet. It is drafted once the deal is qualified (budget, products, dates, signer).")


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
