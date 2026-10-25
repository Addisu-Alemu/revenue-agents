"""Pipeline board: every deal by stage, with tier, score and flags."""

from __future__ import annotations

import streamlit as st

from revenue_agents.models import Stage
from revenue_agents.ui import components, context

COLUMNS = [
    ("Prospecting", (Stage.PROSPECT, Stage.ENRICHED, Stage.SCORED)),
    ("Outreach", (Stage.OUTREACH,)),
    ("Engaged", (Stage.REPLIED, Stage.MEETING)),
    ("Qualified", (Stage.QUALIFIED, Stage.PROPOSAL)),
    ("Signature", (Stage.SIGNATURE,)),
    ("Won", (Stage.HANDOFF,)),
    ("Lost", (Stage.CLOSED_LOST,)),
]


def render() -> None:
    system = context.get_system()
    components.page_header("Pipeline", "Deal state is owned by the orchestrator. A deal only moves when its "
                                       "stage's exit criteria are met.")
    deals = components.segment_deals(system)
    st.caption(f"Showing **{st.session_state.segment}** deals · change it in the sidebar")
    if not deals:
        st.info("No deals yet. Start the world on the Today page: leads arrive on their own.", icon=":material/info:")
        return

    for column, (title, stages) in zip(st.columns(len(COLUMNS)), COLUMNS):
        in_column = [deal for deal in deals if deal.stage in stages]
        with column:
            st.markdown(f"<div class='ra-column-title'>{title} · {len(in_column)}</div>", unsafe_allow_html=True)
            for deal in in_column:
                lead = system.crm.get_lead(deal.lead_id)
                with st.container(border=True):
                    st.markdown(f"**{lead.company}**")
                    st.caption(f"{deal.id} · {lead.segment} · {components.STAGE_LABELS[deal.stage]}")
                    components.tier_badge(deal)
                    components.flag_badges(deal.flags)
                    if st.button("Open", key=f"open-{deal.id}", icon=":material/arrow_forward:", type="tertiary"):
                        st.session_state.selected_deal = deal.id
                        st.switch_page(st.session_state.pages["deal"])
