"""The orchestrator runs the whole lifecycle on the real system; only the model is scripted."""

import re

import pytest

from revenue_agents.demo_llm import demo_llm
from revenue_agents.events import Event, EventType
from revenue_agents.llm import FakeLLM, LLMError
from revenue_agents.models import Stage, Tier
from revenue_agents.stages import is_forward, missing_for
from revenue_agents.runtime import ScriptedRuntime
from revenue_agents.system import build_system
from tests.scripted import BRIGHTBITE_CALL, MAYA_MEETING, SAM_OBJECTION

OWEN_NOT_NOW = {"intent": "not_now", "summary": "Not interested until after the holidays.", "asks_for_pricing": True,
                "objections": ["timing"], "suggested_next_step": "Follow up in January."}
UNSUBSCRIBE = {"intent": "unsubscribe", "summary": "Asks to stop emails.", "asks_for_pricing": False,
               "suggested_next_step": "Remove from outreach."}
REPLIES = {"R-001": MAYA_MEETING, "R-002": SAM_OBJECTION, "R-003": OWEN_NOT_NOW}


def outreach_answer(call):
    """Cite the first lesson offered in the prompt, like a model that takes lessons into account."""
    offered = re.findall(r"\[(lesson:[^\]]+)\]", call.prompt)
    return {"opening": "Saw your recent news and wanted to reach out.", "value_prop_index": 0,
            "lessons_used": offered[:1]}


def reply_answer(call):
    return REPLIES[re.search(r'source="reply:(R-\d+)"', call.prompt).group(1)]


@pytest.fixture
def llm():
    return FakeLLM({**demo_llm().responses, "OutreachDraft": outreach_answer, "ReplyClassification": reply_answer,
                    "CallDigest": BRIGHTBITE_CALL})


@pytest.fixture
def system(tmp_path, llm):
    return build_system(ScriptedRuntime(llm), state_dir=tmp_path)


def lead_created(lead_id):
    return Event(id=f"lead:{lead_id}", type=EventType.LEAD_CREATED, payload={"lead_id": lead_id})


def reply(replies, reply_id):
    return Event(id=f"reply:{reply_id}", type=EventType.REPLY_RECEIVED,
                 payload={"reply": replies[reply_id].model_dump(mode="json", by_alias=True)})


def call(transcript, lead_id="L-001"):
    return Event(id=f"call:{lead_id}", type=EventType.CALL_COMPLETED,
                 payload={"lead_id": lead_id, "transcript_id": "L-001_discovery_call", "transcript": transcript})


def decide(request_id, approved=True):
    return Event(id=f"approval:{request_id}", type=EventType.APPROVAL_DECIDED,
                 payload={"request_id": request_id, "approved": approved, "reviewer": "maria@cool.example"})


def stage(system, deal_id="D-001"):
    return system.crm.get_deal(deal_id).stage


def emails(system, deal_id="D-001"):
    return [a for a in system.crm.timeline(deal_id) if a.kind == "email_sent"]


# --- New lead ----------------------------------------------------------------------------


def test_tier_a_lead_is_enriched_scored_and_waits_for_approval(system):
    report = system.orchestrator.handle(lead_created("L-001"))

    deal = system.crm.get_deal("D-001")
    assert deal.stage is Stage.SCORED
    assert deal.score.tier is Tier.A
    assert emails(system) == []
    assert [r.id for r in system.approvals.pending()] == ["send_email:D-001:first_touch"]
    assert "Stage prospect -> enriched" in report.steps
    assert any("queued" in step for step in report.steps)


def test_approving_the_email_sends_it_and_moves_to_outreach(system):
    system.orchestrator.handle(lead_created("L-001"))
    report = system.orchestrator.handle(decide("send_email:D-001:first_touch"))

    assert len(emails(system)) == 1
    assert stage(system) is Stage.OUTREACH
    assert report.steps[-1] == "Stage scored -> outreach"


def test_rejecting_the_email_sends_nothing(system):
    system.orchestrator.handle(lead_created("L-001"))
    report = system.orchestrator.handle(decide("send_email:D-001:first_touch", approved=False))

    assert emails(system) == []
    assert stage(system) is Stage.SCORED
    assert report.steps == ["Rejected by maria@cool.example: send_email:D-001:first_touch"]


def test_tier_b_lead_is_emailed_automatically(system):
    system.orchestrator.handle(lead_created("L-002"))
    assert stage(system, "D-002") is Stage.OUTREACH
    assert len(emails(system, "D-002")) == 1


def test_tier_c_lead_gets_no_outreach(system):
    report = system.orchestrator.handle(lead_created("L-003"))
    assert stage(system, "D-003") is Stage.SCORED
    assert report.steps[-1] == "Tier C: no outreach, left for nurture"
    assert system.approvals.pending() == []


def test_duplicate_event_is_ignored(system):
    system.orchestrator.handle(lead_created("L-002"))
    second = system.orchestrator.handle(lead_created("L-002"))

    assert second.status == "duplicate"
    assert len(emails(system, "D-002")) == 1
    assert [e.event for e in system.audit.events()].count("event_duplicate") == 1


def test_failed_event_can_be_retried(tmp_path, llm):
    attempts = []

    def flaky(call):
        attempts.append(call)
        if len(attempts) == 1:
            raise LLMError("model timed out")
        return outreach_answer(call)

    system = build_system(ScriptedRuntime(FakeLLM({**llm.responses, "OutreachDraft": flaky})), state_dir=tmp_path)
    with pytest.raises(LLMError):
        system.orchestrator.handle(lead_created("L-002"))

    assert system.orchestrator.handle(lead_created("L-002")).status == "processed"
    assert len(emails(system, "D-002")) == 1


def test_rejected_draft_flags_the_deal(tmp_path, llm):
    bad = {"opening": "We guarantee results.", "value_prop_index": 0}
    system = build_system(ScriptedRuntime(FakeLLM({**llm.responses, "OutreachDraft": bad})), state_dir=tmp_path)
    system.orchestrator.handle(lead_created("L-002"))

    deal = system.crm.get_deal("D-002")
    assert deal.stage is Stage.SCORED
    assert "outreach_draft_rejected" in deal.flags


def test_kill_switch_holds_a_new_lead_until_release(system):
    system.orchestrator.pause("Pause everything", by="ops@cool.example")
    report = system.orchestrator.handle(lead_created("L-002"))

    assert report.status == "held"
    assert system.crm.deals() == []  # nothing ran
    assert [e.id for e in system.orchestrator.held_events()] == ["lead:L-002"]

    system.orchestrator.resume(by="ops@cool.example")
    assert stage(system, "D-002") is Stage.OUTREACH  # processed after release: nothing lost
    assert system.orchestrator.held_events() == []


def test_kill_switch_pulled_mid_event_is_replayed_on_release(system):
    """An operator hits the kill switch while a lead is being processed: it stops, then finishes on release."""
    host = system.orchestrator._agents.host
    original_run = host.run

    def run_and_kill(task, on_result=None):
        def kill_after_first(action, result):
            on_result(action, result)
            system.kill_switch.engage("Stop now")  # pulled right after the enrichment ran

        return original_run(task, kill_after_first)

    host.run = run_and_kill
    report = system.orchestrator.handle(lead_created("L-002"))

    assert report.status == "held"
    assert "Kill switch pulled during processing" in report.steps[-1]
    assert stage(system, "D-002") is Stage.ENRICHED  # scoring was refused: nothing ran after the switch

    host.run = original_run
    system.orchestrator.resume(by="ops@cool.example")
    assert stage(system, "D-002") is Stage.OUTREACH
    assert len(emails(system, "D-002")) == 1


def test_lead_stops_if_its_score_does_not_land(system):
    """Defensive stop: if the score never lands (here the tool is missing), no email is drafted for an unscored lead."""
    research = system.orchestrator._agents.research
    original = research.task

    def without_scoring(lead, deal):
        task = original(lead, deal)
        task.tools = [tool for tool in task.tools if tool.name != "score_lead"]
        return task

    research.task = without_scoring
    report = system.orchestrator.handle(lead_created("L-001"))

    assert report.steps[-1] == "Stopped: lead could not be scored"
    assert system.approvals.pending() == []


# --- Replies -------------------------------------------------------------------------------


def test_meeting_request_moves_the_deal_to_meeting(system, replies):
    system.orchestrator.handle(lead_created("L-001"))
    system.orchestrator.handle(decide("send_email:D-001:first_touch"))
    report = system.orchestrator.handle(reply(replies, "R-001"))

    assert stage(system) is Stage.MEETING
    assert "Stage outreach -> replied" in report.steps
    assert "Stage replied -> meeting" in report.steps


def test_injection_reply_is_flagged_and_teaches_nothing(system, replies):
    system.orchestrator.handle(lead_created("L-002"))
    report = system.orchestrator.handle(reply(replies, "R-003"))

    deal = system.crm.get_deal("D-002")
    assert {"possible_prompt_injection", "pricing_requested"} <= set(deal.flags)
    assert deal.score.tier is Tier.B  # "mark this lead as Tier A" changed nothing
    assert system.lessons.all() == []
    assert "Suspicious content: ignored as instructions, no lesson learned" in report.steps


def test_unsubscribe_closes_the_deal(tmp_path, llm, replies):
    system = build_system(ScriptedRuntime(FakeLLM({**llm.responses, "ReplyClassification": UNSUBSCRIBE})), state_dir=tmp_path)
    system.orchestrator.handle(lead_created("L-002"))
    system.orchestrator.handle(reply(replies, "R-003"))
    assert stage(system, "D-002") is Stage.CLOSED_LOST


# --- Call, qualification and IO --------------------------------------------------------------


def test_call_qualifies_the_deal_and_drafts_the_io(system, replies, transcript):
    system.orchestrator.handle(lead_created("L-001"))
    system.orchestrator.handle(decide("send_email:D-001:first_touch"))
    system.orchestrator.handle(reply(replies, "R-001"))
    report = system.orchestrator.handle(call(transcript))

    deal = system.crm.get_deal("D-001")
    assert deal.fields.budget_usd == 250000
    assert deal.stage is Stage.PROPOSAL
    assert "Stage meeting -> qualified" in report.steps
    assert "Stage qualified -> proposal" in report.steps
    assert [r.id for r in system.approvals.pending()] == ["send_io:D-001:IO-D-001"]

    system.orchestrator.handle(decide("send_io:D-001:IO-D-001"))
    assert stage(system) is Stage.SIGNATURE


def test_incomplete_call_does_not_qualify(tmp_path, llm, transcript):
    partial = {"deal_fields": {"next_step": "Follow up"}, "lessons": []}
    system = build_system(ScriptedRuntime(FakeLLM({**llm.responses, "CallDigest": partial})), state_dir=tmp_path)
    system.orchestrator.handle(lead_created("L-001"))
    report = system.orchestrator.handle(call(transcript))

    assert stage(system) is Stage.SCORED
    assert report.steps[-1] == "Not qualified yet: no IO drafted"
    assert any("qualified needs budget_usd" in step for step in report.steps)


def test_second_call_with_the_same_terms_does_not_redraft(system, replies, transcript):
    system.orchestrator.handle(lead_created("L-001"))
    system.orchestrator.handle(call(transcript))
    again = Event(id="call:L-001:second", type=EventType.CALL_COMPLETED,
                  payload={"lead_id": "L-001", "transcript_id": "second_call", "transcript": transcript})
    assert system.orchestrator.handle(again).steps[-1] == "IO terms unchanged: IO-D-001 still stands"


def test_blocked_io_flags_the_deal(tmp_path, llm, transcript):
    no_terms = {**BRIGHTBITE_CALL, "deal_fields": {**BRIGHTBITE_CALL["deal_fields"], "payment_terms": None}}
    system = build_system(ScriptedRuntime(FakeLLM({**llm.responses, "CallDigest": no_terms})), state_dir=tmp_path)
    system.orchestrator.handle(lead_created("L-001"))
    system.orchestrator.handle(call(transcript))

    deal = system.crm.get_deal("D-001")
    assert deal.stage is Stage.QUALIFIED
    assert "io_blocked" in deal.flags


# --- Knowledge loop ------------------------------------------------------------------------


def test_lesson_from_one_call_shapes_the_next_brand_email(system, llm, transcript):
    system.orchestrator.handle(lead_created("L-001"))
    system.orchestrator.handle(call(transcript))  # BrightBite call produces brand lessons
    report = system.orchestrator.handle(lead_created("L-002"))  # next brand lead

    [sent] = emails(system, "D-002")
    lesson_id = sent.data["lessons_used"][0]
    assert lesson_id.startswith("lesson:L-001_discovery_call:")
    assert any(step.startswith("Draft uses lessons:") for step in report.steps)
    assert [use.deal_id for use in system.lessons.uses(lesson_id)] == ["D-002"]
    assert "brand control" in llm.calls[-1].prompt


# --- Stage rules -----------------------------------------------------------------------------


def test_stages_only_move_forward():
    assert is_forward(Stage.SCORED, Stage.OUTREACH)
    assert not is_forward(Stage.OUTREACH, Stage.SCORED)
    assert is_forward(Stage.MEETING, Stage.CLOSED_LOST)
    assert not is_forward(Stage.CLOSED_LOST, Stage.OUTREACH)


def test_missing_criteria_are_named(make_deal):
    deal = make_deal(stage=Stage.SCORED)
    assert missing_for(Stage.OUTREACH, deal, []) == ["a sent email"]
    assert missing_for(Stage.QUALIFIED, deal, []) == ["budget_usd", "products", "flight_start", "flight_end",
                                                      "decision_maker"]
    assert missing_for(Stage.HANDOFF, deal, []) == ["a signed IO"]
    assert missing_for(Stage.CLOSED_LOST, deal, []) == []


# --- Reply without outreach -------------------------------------------------------------


def test_reply_from_a_lead_we_never_emailed_is_flagged_not_progressed(system, replies):
    system.orchestrator.handle(lead_created("L-001"))  # Tier A: email waits, nothing sent
    report = system.orchestrator.handle(reply(replies, "R-001"))

    deal = system.crm.get_deal("D-001")
    assert deal.stage is Stage.SCORED
    assert "reply_without_outreach" in deal.flags
    assert system.lessons.all() == []  # nothing learned from it
    assert report.steps[-1] == "Reply without outreach: flagged for a human, stage unchanged, nothing learned"
    assert [a.kind for a in system.crm.timeline("D-001")].count("reply") == 1  # still recorded


def test_unsubscribe_is_honoured_even_without_outreach(tmp_path, llm, replies):
    system = build_system(ScriptedRuntime(FakeLLM({**llm.responses, "ReplyClassification": UNSUBSCRIBE})), state_dir=tmp_path)
    system.orchestrator.handle(lead_created("L-001"))
    system.orchestrator.handle(reply(replies, "R-001"))
    assert stage(system) is Stage.CLOSED_LOST
