"""Agents -> hook -> gateway -> handlers -> CRM: what the agents do really changes the system of record."""

import datetime as dt

from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.guardrails.gateway import Outcome
from revenue_agents.guardrails.policy import Action, ActionType
from revenue_agents.models import Stage, Tier
from tests.desk import agents, desk, emailed, place, run
from tests.scripted import BRIGHTBITE_CALL, MAYA_MEETING, SAM_OBJECTION


def research(system, lead_id="L-001"):
    lead = system.crm.get_lead(lead_id)
    run(system, agents(system).research.task(lead, system.crm.deal_for_lead(lead_id)))
    return lead, system.crm.get_deal(system.crm.deal_for_lead(lead_id).id)


def test_enrichment_score_and_research_note_are_saved_on_the_deal(tmp_path):
    system, _ = desk(tmp_path)
    _, deal = research(system)

    assert deal.enrichment.locations == 420
    assert (deal.score.tier, deal.score.total) == (Tier.A, 97.0)
    assert [a.kind for a in system.crm.timeline(deal.id)] == ["note", "note", "research"]


def test_tier_a_email_waits_then_is_sent_after_approval(tmp_path, transcript):
    system, _ = desk(tmp_path, CallDigest=BRIGHTBITE_CALL)
    lead, deal = research(system)
    run(system, agents(system).call_intel.task("call-1", transcript, lead, deal))  # lessons to cite
    known_lesson = system.lessons.for_segment("brand")[0].id
    system.runtime.answers.responses["OutreachDraft"] = {
        "opening": "Congrats on the expansion.", "value_prop_index": 0, "lessons_used": [known_lesson]}

    result = run(system, agents(system).outreach.first_touch(lead, system.crm.get_deal(deal.id)))
    assert [r.outcome for _, r in result.results()] == [Outcome.QUEUED]
    assert not [a for a in system.crm.timeline(deal.id) if a.kind == "email_sent"]

    system.approvals.approve("send_email:D-001:first_touch", reviewer="maria@cool.example")
    assert system.gateway.execute_approved("send_email:D-001:first_touch", deal).outcome is Outcome.EXECUTED

    [sent] = [a for a in system.crm.timeline(deal.id) if a.kind == "email_sent"]
    assert sent.data["to"] == "maya.thompson@brightbite.example"
    assert [u.used_in for u in system.lessons.uses(known_lesson)] == ["send_email:D-001:first_touch"]


def test_tier_b_email_is_sent_straight_away(tmp_path):
    system, _ = desk(tmp_path)
    lead, deal = research(system, "L-002")
    assert deal.score.tier is Tier.B

    result = run(system, agents(system).outreach.first_touch(lead, deal))
    assert [r.outcome for _, r in result.results()] == [Outcome.EXECUTED]
    assert [a.kind for a in system.crm.timeline(deal.id)][-1] == "email_sent"


def test_reply_is_logged_flagged_booked_and_its_lesson_saved(tmp_path, replies):
    system, _ = desk(tmp_path, ReplyClassification=MAYA_MEETING)
    deal = place(system, "L-001", Stage.OUTREACH)
    run(system, agents(system).reply.task(replies["R-001"], system.crm.get_lead("L-001"), deal, emailed(system, deal.id)))

    assert system.crm.get_deal(deal.id).flags == ["meeting_requested"]  # set in code from the classification
    assert [a.kind for a in system.crm.timeline(deal.id)] == ["email_sent", "reply", "note", "meeting_booked"]
    assert system.lessons.all()[0].source_id == "R-001"


def test_flags_are_not_duplicated(tmp_path, replies):
    system, _ = desk(tmp_path, ReplyClassification=SAM_OBJECTION)
    deal = place(system, "L-005", Stage.OUTREACH)
    run(system, agents(system).reply.task(replies["R-002"], system.crm.get_lead("L-005"), deal, emailed(system, deal.id)))

    again = Action(id="flag_deal:D-005:again", type=ActionType.FLAG_DEAL, agent="reply", deal_id=deal.id,
                   summary="Flags again", payload={"flags": ["objection", "pricing_requested"]})
    system.gateway.submit(again, system.crm.get_deal(deal.id))
    assert system.crm.get_deal(deal.id).flags == ["objection", "pricing_requested"]


def test_a_follow_up_is_a_dated_timer_on_the_deal(tmp_path, replies):
    system, _ = desk(tmp_path, ReplyClassification=SAM_OBJECTION)
    deal = place(system, "L-005", Stage.OUTREACH)
    run(system, agents(system).reply.task(replies["R-002"], system.crm.get_lead("L-005"), deal, emailed(system, deal.id)))

    [timer] = [a for a in system.crm.timeline(deal.id) if a.kind == "follow_up"]
    assert (timer.data["due"], timer.data["reply_id"]) == ("2027-01-05", "R-002")


def test_call_fields_merge_without_erasing_known_facts(tmp_path):
    partial = {"deal_fields": {**BRIGHTBITE_CALL["deal_fields"], "payment_terms": None}, "lessons": ["One lesson."]}
    system, _ = desk(tmp_path, CallDigest=partial)
    deal = place(system, "L-001", Stage.MEETING)
    system.crm.save_deal(deal.model_copy(update={"fields": deal.fields.model_copy(update={"payment_terms": "Net 45"})}))
    run(system, agents(system).call_intel.task("call-1", "transcript", system.crm.get_lead("L-001"),
                                               system.crm.get_deal(deal.id)))

    fields = system.crm.get_deal(deal.id).fields
    assert fields.budget_usd == 250000
    assert fields.payment_terms == "Net 45"  # not wiped by an empty extraction
    assert [lesson.text for lesson in system.lessons.all()] == ["One lesson."]


def test_io_draft_is_attached_and_sending_waits_for_approval(tmp_path, segments):
    system, _ = desk(tmp_path, CallDigest=BRIGHTBITE_CALL)
    deal = place(system, "L-001", Stage.MEETING)
    lead = system.crm.get_lead("L-001")
    run(system, agents(system).call_intel.task("call-1", "transcript", lead, deal))
    deal = system.crm.get_deal(deal.id).model_copy(update={"stage": Stage.QUALIFIED})
    system.crm.save_deal(deal)

    result = DealDeskAgent(segments).run(lead, deal, today=dt.date(2026, 10, 10))
    outcomes = [system.gateway.submit(action, system.crm.get_deal(deal.id)).outcome for action in result.actions]
    assert outcomes == [Outcome.EXECUTED, Outcome.QUEUED]

    [document] = [a for a in system.crm.timeline(deal.id) if a.kind == "document"]
    assert "**Total budget:** 250,000 USD" in document.data["document"]
    assert [r.id for r in system.approvals.pending()] == ["send_io:D-001:IO-D-001"]

    system.approvals.approve("send_io:D-001:IO-D-001", reviewer="maria@cool.example")
    assert system.gateway.execute_approved("send_io:D-001:IO-D-001", deal).result["envelope_id"] == "ENV-IO-D-001"
    sent = system.crm.timeline(deal.id)[-1]
    assert (sent.kind, sent.data["envelope_id"]) == ("io_sent", "ENV-IO-D-001")
