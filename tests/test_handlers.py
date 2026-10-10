"""Agents -> gateway -> handlers -> CRM: approved actions really change the system of record."""

import datetime as dt

import pytest

from revenue_agents.agents.call_intel import CallIntelAgent
from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.agents.outreach import OutreachAgent
from revenue_agents.agents.reply_triage import ReplyTriageAgent
from revenue_agents.agents.research import ResearchAgent
from revenue_agents.agents.scoring import ScoringAgent
from revenue_agents.crm import MockHubSpot
from revenue_agents.esign import MockESign
from revenue_agents.guardrails.approvals import ApprovalQueue
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.gateway import ActionGateway, Outcome
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import AutonomyPolicy
from revenue_agents.handlers import ActionHandlers
from revenue_agents.lessons import LessonStore
from revenue_agents.llm import FakeLLM
from revenue_agents.models import Stage, Tier
from tests.scripted import BRIGHTBITE_CALL, MAYA_MEETING, SAM_OBJECTION


@pytest.fixture
def system(tmp_path, segments):
    crm = MockHubSpot(tmp_path / "hubspot.json")
    lessons = LessonStore(tmp_path / "lessons.json")
    approvals = ApprovalQueue(tmp_path / "approvals.json")
    gateway = ActionGateway(
        AutonomyPolicy(segments, KillSwitch(tmp_path / "KILL_SWITCH")), approvals, AuditLog(tmp_path / "audit.jsonl"),
        ActionHandlers(crm, lessons, MockESign(tmp_path / "esign.json")).as_dict(), ActionClaims(tmp_path / "claims"),
    )
    return crm, lessons, approvals, gateway


def submit_all(gateway, crm, result):
    return [gateway.submit(action, crm.get_deal(action.deal_id)).outcome for action in result.actions]


def enrich_and_score(system, enricher, segments, lead_id="L-001"):
    crm, _, _, gateway = system
    lead, deal = crm.get_lead(lead_id), crm.deal_for_lead(lead_id)
    submit_all(gateway, crm, ResearchAgent(enricher).run(lead, deal))
    enrichment = crm.get_deal(deal.id).enrichment
    submit_all(gateway, crm, ScoringAgent(segments).run(lead, crm.get_deal(deal.id), enrichment))
    return lead, crm.get_deal(deal.id)


def test_enrichment_and_score_are_saved_on_the_deal(system, enricher, segments):
    _, deal = enrich_and_score(system, enricher, segments)
    assert deal.enrichment.locations == 420
    assert deal.score.tier is Tier.A
    assert deal.score.total == 97.0


def test_tier_a_email_waits_then_is_sent_after_approval(system, enricher, segments):
    crm, lessons, approvals, gateway = system
    lead, deal = enrich_and_score(system, enricher, segments)
    for lesson_action in CallIntelAgent(FakeLLM({"CallDigest": BRIGHTBITE_CALL}), segments).run(
            "call-1", "transcript", lead, deal).actions[1:]:
        gateway.submit(lesson_action, deal)
    known_lesson = lessons.for_segment("brand")[0].id

    llm = FakeLLM({"OutreachDraft": {"opening": "Congrats on the expansion.", "value_prop_index": 0,
                                     "lessons_used": [known_lesson]}})
    result = OutreachAgent(llm, segments).run(lead, deal, deal.enrichment, lessons.for_segment("brand"))

    assert submit_all(gateway, crm, result) == [Outcome.QUEUED]
    assert not [a for a in crm.timeline(deal.id) if a.kind == "email_sent"]

    approvals.approve("send_email:D-001:first_touch", reviewer="maria@cool.example")
    assert gateway.execute_approved("send_email:D-001:first_touch", deal).outcome is Outcome.EXECUTED

    [sent] = [a for a in crm.timeline(deal.id) if a.kind == "email_sent"]
    assert sent.data["to"] == "maya.thompson@brightbite.example"
    assert [u.used_in for u in lessons.uses(known_lesson)] == ["send_email:D-001:first_touch"]


def test_tier_b_email_is_sent_straight_away(system, enricher, segments):
    crm, _, _, gateway = system
    lead, deal = enrich_and_score(system, enricher, segments, lead_id="L-002")
    assert deal.score.tier is Tier.B

    llm = FakeLLM({"OutreachDraft": {"opening": "Saw your holiday promotion.", "value_prop_index": 0}})
    result = OutreachAgent(llm, segments).run(lead, deal, deal.enrichment, [])

    assert submit_all(gateway, crm, result) == [Outcome.EXECUTED]
    assert [a.kind for a in crm.timeline(deal.id)][-1] == "email_sent"


def test_reply_is_logged_flagged_and_its_lesson_saved(system, replies):
    crm, lessons, _, gateway = system
    lead, deal = crm.get_lead("L-001"), crm.deal_for_lead("L-001")
    result = ReplyTriageAgent(FakeLLM({"ReplyClassification": MAYA_MEETING})).run(replies["R-001"], lead, deal)

    assert set(submit_all(gateway, crm, result)) == {Outcome.EXECUTED}
    assert crm.get_deal(deal.id).flags == ["meeting_requested"]
    assert [a.kind for a in crm.timeline(deal.id)] == ["reply", "flag", "note"]
    assert lessons.all()[0].source_id == "R-001"


def test_flags_are_not_duplicated(system, replies):
    crm, _, _, gateway = system
    lead, deal = crm.get_lead("L-005"), crm.deal_for_lead("L-005")
    agent = ReplyTriageAgent(FakeLLM({"ReplyClassification": SAM_OBJECTION}))
    submit_all(gateway, crm, agent.run(replies["R-002"], lead, deal))

    flag_action = agent.run(replies["R-002"], lead, deal).actions[1].model_copy(update={"id": "flag_deal:D-005:again"})
    gateway.submit(flag_action, crm.get_deal(deal.id))
    assert crm.get_deal(deal.id).flags == ["objection", "pricing_requested"]


def test_call_fields_merge_without_erasing_known_facts(system, segments):
    crm, lessons, _, gateway = system
    lead, deal = crm.get_lead("L-001"), crm.deal_for_lead("L-001")
    crm.save_deal(deal.model_copy(update={"fields": deal.fields.model_copy(update={"payment_terms": "Net 45"})}))

    partial = {"deal_fields": {**BRIGHTBITE_CALL["deal_fields"], "payment_terms": None}, "lessons": ["One lesson."]}
    result = CallIntelAgent(FakeLLM({"CallDigest": partial}), segments).run("call-1", "transcript", lead, deal)
    submit_all(gateway, crm, result)

    fields = crm.get_deal(deal.id).fields
    assert fields.budget_usd == 250000
    assert fields.payment_terms == "Net 45"  # not wiped by an empty extraction
    assert [l.text for l in lessons.all()] == ["One lesson."]


def test_io_draft_is_attached_and_sending_waits_for_approval(system, segments):
    crm, _, approvals, gateway = system
    lead, deal = crm.get_lead("L-001"), crm.deal_for_lead("L-001")
    fields = CallIntelAgent(FakeLLM({"CallDigest": BRIGHTBITE_CALL}), segments).run(
        "call-1", "transcript", lead, deal).output.fields
    deal = deal.model_copy(update={"fields": fields, "stage": Stage.QUALIFIED})
    crm.save_deal(deal)

    result = DealDeskAgent(segments).run(lead, deal, today=dt.date(2026, 10, 10))
    assert submit_all(gateway, crm, result) == [Outcome.EXECUTED, Outcome.QUEUED]

    [document] = [a for a in crm.timeline(deal.id) if a.kind == "document"]
    assert "**Total budget:** 250,000 USD" in document.data["document"]
    assert [r.id for r in approvals.pending()] == ["send_io:D-001:IO-D-001"]

    approvals.approve("send_io:D-001:IO-D-001", reviewer="maria@cool.example")
    assert gateway.execute_approved("send_io:D-001:IO-D-001", deal).result["envelope_id"] == "ENV-IO-D-001"
    sent = crm.timeline(deal.id)[-1]
    assert (sent.kind, sent.data["envelope_id"]) == ("io_sent", "ENV-IO-D-001")
