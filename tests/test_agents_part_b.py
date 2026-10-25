"""Outreach and deal desk agents.

The outreach agent works with its tools (read the account, search lessons, draft, send) through the hook and the
gateway; its decisions are scripted (FakeLLM). The deal desk is a code skill: contracts must be exact.
"""

import datetime as dt

import pytest

from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import ActionType, AutonomyPolicy, Decision
from revenue_agents.models import DealFields, Lesson, Stage, Tier
from revenue_agents.runtime import RunStatus, Verdict
from tests.desk import agents, desk, place, run, tool_names

GOOD_DRAFT = {
    "opening": "Congratulations on the 60 new franchise locations planned for 2027.",
    "value_prop_index": 1,
    "lessons_used": ["lesson:call-1:0"],
}

BRAND_LESSON = Lesson(id="lesson:call-1:0", segment="brand", source="call", source_id="call-1",
                      text="Franchise brands care more about brand control than speed.",
                      created_at=dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc))
AGENCY_LESSON = BRAND_LESSON.model_copy(update={"id": "lesson:call-2:0", "segment": "agency", "text": "Agency tip."})


@pytest.fixture
def policy(tmp_path, segments):
    return AutonomyPolicy(segments, KillSwitch(tmp_path / "KILL_SWITCH"))


class Drafted:
    """One outreach run: the run, the scripted decisions it asked for, and the email it proposed (if any)."""

    def __init__(self, tmp_path, answer=GOOD_DRAFT, lessons=(BRAND_LESSON,), tier=Tier.A):
        self.system, self.llm = desk(tmp_path, OutreachDraft=answer)
        for lesson in lessons:
            self.system.lessons.add(lesson)
        deal = place(self.system, "L-001", Stage.SCORED, tier)
        self.run = run(self.system, agents(self.system).outreach.first_touch(self.system.crm.get_lead("L-001"), deal))
        sends = [call for call in self.run.called("send_email") if call.action]
        self.send = sends[-1] if sends else None
        self.email = self.send.action.payload if self.send else None
        self.prompts = [call.prompt for call in self.llm.calls if call.schema == "OutreachDraft"]


# --- Outreach ----------------------------------------------------------------------


def test_the_agent_reads_drafts_and_sends(tmp_path):
    drafted = Drafted(tmp_path)
    assert tool_names(drafted.run) == ["read_account", "search_lessons", "draft_email", "send_email"]


def test_email_is_built_from_the_segment_template(tmp_path):
    email = Drafted(tmp_path).email

    assert email["to"] == "maya.thompson@brightbite.example"
    assert email["subject"] == "Local ads for every BrightBite Burgers location"
    assert email["body"].startswith("Hi Maya,")
    assert "60 new franchise locations" in email["body"]
    assert "Franchisees can only change the fields corporate allows" in email["body"]  # value prop #1 from brand.yaml
    assert "a 30-minute call next week" in email["body"]
    assert email["body"].rstrip().endswith("Daniel Reyes")


def test_tier_a_first_email_is_deferred_to_a_human(tmp_path):
    drafted = Drafted(tmp_path, tier=Tier.A)

    assert drafted.send.action.id == "send_email:D-001:first_touch"
    assert (drafted.send.verdict, drafted.send.rule) == (Verdict.DEFER, "first_touch_tier")
    assert drafted.run.status is RunStatus.WAITING
    assert [request.id for request in drafted.system.approvals.pending()] == ["send_email:D-001:first_touch"]
    assert not [a for a in drafted.system.crm.timeline("D-001") if a.kind == "email_sent"]


def test_tier_b_first_email_goes_out_on_its_own(tmp_path):
    drafted = Drafted(tmp_path, tier=Tier.B)

    assert (drafted.send.verdict, drafted.send.outcome.value) == (Verdict.ALLOW, "executed")
    assert [a.kind for a in drafted.system.crm.timeline("D-001") if a.kind == "email_sent"] == ["email_sent"]


def test_sending_without_an_accepted_draft_is_refused(tmp_path):
    system, _ = desk(tmp_path)
    deal = place(system, "L-001", Stage.SCORED, Tier.B)
    task = agents(system).outreach.first_touch(system.crm.get_lead("L-001"), deal)
    task.script = lambda s: s.call("send_email")
    [call] = run(system, task).calls

    assert (call.verdict, call.rule) == (Verdict.DENY, "tool_rule")
    assert "draft_email first" in call.reason


def test_lessons_are_offered_and_their_use_is_recorded(tmp_path):
    drafted = Drafted(tmp_path, lessons=(BRAND_LESSON, AGENCY_LESSON))

    assert "[lesson:call-1:0] Franchise brands care more about brand control" in drafted.prompts[0]
    assert "Agency tip." not in drafted.prompts[0]  # other segment's lessons are not offered
    assert drafted.email["lessons_used"] == ["lesson:call-1:0"]


def test_global_lessons_from_another_segment_are_offered(tmp_path):
    shared = AGENCY_LESSON.model_copy(update={"id": "lesson:call-2:g0", "text": "Offer a short pilot.", "scope": "global"})
    drafted = Drafted(tmp_path, answer={**GOOD_DRAFT, "lessons_used": ["lesson:call-2:g0"]},
                      lessons=(AGENCY_LESSON, shared))

    assert "[lesson:call-2:g0] Offer a short pilot." in drafted.prompts[0]
    assert "Agency tip." not in drafted.prompts[0]
    assert drafted.email["lessons_used"] == ["lesson:call-2:g0"]


def test_model_cannot_cite_lessons_it_was_not_given(tmp_path):
    drafted = Drafted(tmp_path, answer={**GOOD_DRAFT, "lessons_used": ["lesson:call-1:0", "lesson:made-up"]})
    assert drafted.email["lessons_used"] == ["lesson:call-1:0"]


def test_without_lessons_the_agent_is_told_none_yet(tmp_path):
    assert "- (none yet)" in Drafted(tmp_path, lessons=()).prompts[0]


def test_enrichment_facts_are_wrapped_as_untrusted(tmp_path):
    assert '<untrusted source="enrichment:brightbite.example">' in Drafted(tmp_path).prompts[0]


def test_the_research_note_reaches_the_writer(tmp_path):
    system, llm = desk(tmp_path)
    lead = system.crm.get_lead("L-001")
    run(system, agents(system).research.task(lead, system.crm.deal_for_lead("L-001")))
    run(system, agents(system).outreach.first_touch(lead, system.crm.get_deal("D-001")))
    prompt = next(call.prompt for call in llm.calls if call.schema == "OutreachDraft")
    assert "Why now: 60 new franchise locations" in prompt


def test_forbidden_phrase_is_handed_back_and_fixed(tmp_path):
    answers = iter([{**GOOD_DRAFT, "opening": "We guarantee results for all 420 locations."}, GOOD_DRAFT])
    drafted = Drafted(tmp_path, answer=lambda call: next(answers))

    assert len(drafted.prompts) == 2
    assert drafted.run.called("draft_email")[0].is_error
    assert "forbidden phrases ['guarantee']" in drafted.prompts[1]
    assert "guarantee" not in drafted.email["body"].lower()


def test_no_email_after_repeated_violations(tmp_path):
    drafted = Drafted(tmp_path, answer={**GOOD_DRAFT, "opening": "Huge DISCOUNT for early movers!"})

    assert drafted.send is None
    assert "forbidden phrases ['discount']" in drafted.run.called("draft_email")[-1].result
    assert "No acceptable draft" in drafted.run.summary


def test_invalid_value_prop_choice_is_rejected(tmp_path):
    drafted = Drafted(tmp_path, answer={**GOOD_DRAFT, "value_prop_index": 7})
    assert drafted.send is None
    assert "value_prop_index must be 0-2" in drafted.run.called("draft_email")[-1].result


def test_forbidden_phrase_check_matches_whole_words_only(tmp_path):
    drafted = Drafted(tmp_path, answer={**GOOD_DRAFT, "opening": "Your guaranteed-delivery promise caught our eye."})
    assert "guaranteed-delivery" in drafted.email["body"]  # "guaranteed" != "guarantee"


# --- Deal desk ---------------------------------------------------------------------

BRIGHTBITE_FIELDS = DealFields(
    budget_usd=250000,
    products=["Create: Dynamic Creative Optimization", "Activate: Franchise & Location Targeting"],
    scope="Pilot for 120 Southeast locations",
    flight_start=dt.date(2027, 1, 15),
    flight_end=dt.date(2027, 4, 15),
    decision_maker="Ken Adeyemi (CFO)",
    payment_terms="Net 30",
    contract_term="3-month pilot with option to extend",
)


def io_for(leads, segments, make_deal, fields=BRIGHTBITE_FIELDS):
    deal = make_deal(stage=Stage.QUALIFIED).model_copy(update={"fields": fields})
    return DealDeskAgent(segments).run(leads["L-001"], deal, today=dt.date(2026, 10, 10)), deal


def test_io_is_generated_from_deal_data(leads, segments, make_deal):
    result, _ = io_for(leads, segments, make_deal)
    doc = result.output.document

    assert result.output.errors == []
    assert "# Insertion Order IO-D-001" in doc
    assert "**Date issued:** 2026-10-10" in doc
    assert "Ken Adeyemi (CFO)" in doc
    assert "2027-01-15 to 2027-04-15" in doc
    assert "**Total budget:** 250,000 USD" in doc
    assert "**Payment terms:** Net 30" in doc
    assert "{{" not in doc


def test_line_items_add_up_to_the_budget(leads, segments, make_deal):
    fields = BRIGHTBITE_FIELDS.model_copy(update={"budget_usd": 100000, "products": [
        "Create: Dynamic Creative Optimization", "Activate: Franchise & Location Targeting",
        "Optimize: Location-Level Measurement"]})
    doc = io_for(leads, segments, make_deal, fields)[0].output.document
    assert "| Create: Dynamic Creative Optimization | 33,334 USD |" in doc
    assert doc.count("| 33,333 USD |") == 2


def test_draft_is_automatic_but_sending_needs_approval(leads, segments, make_deal, policy):
    result, deal = io_for(leads, segments, make_deal)
    draft_action, send_action = result.actions

    assert draft_action.type is ActionType.DRAFT_IO
    assert policy.decide(draft_action, deal).decision is Decision.AUTO
    assert send_action.type is ActionType.SEND_IO
    assert policy.decide(send_action, deal).decision is Decision.NEEDS_APPROVAL


def test_incomplete_deal_gets_no_io_and_a_list_of_gaps(leads, segments, make_deal):
    result, _ = io_for(leads, segments, make_deal, DealFields(budget_usd=0))
    assert result.output.document is None
    assert result.actions == []
    assert result.output.errors == ["budget is missing", "no products selected", "flight dates are missing",
                                    "signatory (decision maker) is missing", "payment terms are missing"]


def test_io_rejects_products_outside_the_segment_and_bad_dates(leads, segments, make_deal):
    fields = BRIGHTBITE_FIELDS.model_copy(update={"products": ["Magic Billboards"], "flight_end": dt.date(2027, 1, 1)})
    errors = io_for(leads, segments, make_deal, fields)[0].output.errors
    assert "products not offered to this segment: ['Magic Billboards']" in errors
    assert "flight end must be after flight start" in errors


def test_unfilled_template_fields_block_the_io(tmp_path, leads, segments, make_deal):
    template = tmp_path / "io.md"
    template.write_text("IO {{io_number}} for {{advertiser_name}}, discount: {{secret_discount}}")
    deal = make_deal(stage=Stage.QUALIFIED).model_copy(update={"fields": BRIGHTBITE_FIELDS})

    result = DealDeskAgent(segments, template=template).run(leads["L-001"], deal)
    assert result.output.errors == ["unfilled template fields: ['secret_discount']"]
    assert result.actions == []


@pytest.mark.parametrize(
    "opening",
    [
        "Hi Maya, congratulations on the 60 new BrightBite locations planned for 2027.",
        "Hello Maya! Congratulations on the 60 new BrightBite locations planned for 2027.",
        "Dear Maya Thompson, congratulations on the 60 new BrightBite locations planned for 2027.",
        "hi maya — congratulations on the 60 new BrightBite locations planned for 2027.",
    ],
)
def test_a_greeting_from_the_model_is_not_doubled(tmp_path, opening):
    """Found on the first real Claude run: the template greets, so the model's own greeting is removed."""
    body = Drafted(tmp_path, answer={**GOOD_DRAFT, "opening": opening}).email["body"]
    assert body.count("Maya") == 1
    assert "Hi Maya,\n\nCongratulations on the 60 new BrightBite locations" in body


def test_words_that_merely_start_like_a_greeting_are_kept(tmp_path):
    opening = "Highlights from your expansion caught our eye."
    assert opening in Drafted(tmp_path, answer={**GOOD_DRAFT, "opening": opening}).email["body"]


def test_prompt_tells_the_model_not_to_greet_or_repeat_the_value_prop(tmp_path):
    """Found on real Claude runs: a doubled greeting, and an opening that restated the value prop below it."""
    system = Drafted(tmp_path).llm.calls[0].system
    assert "Don't start with a greeting" in system
    assert "Don't describe what Cool AI does or restate a value" in system


# --- Lessons must be visibly used -------------------------------------------------------


def test_a_draft_that_ignores_offered_lessons_is_asked_once_to_apply_one(tmp_path):
    """Found on a real Claude run: offered five lessons, it cited none, so the loop never showed."""
    answers = iter([{**GOOD_DRAFT, "lessons_used": []}, GOOD_DRAFT])
    drafted = Drafted(tmp_path, answer=lambda call: next(answers))

    assert len(drafted.prompts) == 2
    assert "applied none" in drafted.prompts[1]
    assert "You applied none of the lessons" in drafted.run.called("draft_email")[0].result
    assert drafted.email["lessons_used"] == ["lesson:call-1:0"]


def test_ignoring_lessons_twice_still_sends_the_email(tmp_path):
    """The nudge improves the email; it never blocks it."""
    drafted = Drafted(tmp_path, answer={**GOOD_DRAFT, "lessons_used": []})

    assert len(drafted.prompts) == 2
    assert drafted.email["lessons_used"] == []
    assert drafted.send.action.type is ActionType.SEND_EMAIL


def test_no_lesson_nudge_when_no_lessons_were_offered(tmp_path):
    assert len(Drafted(tmp_path, answer={**GOOD_DRAFT, "lessons_used": []}, lessons=()).prompts) == 1


def test_lesson_nudge_does_not_use_up_the_forbidden_phrase_retry(tmp_path):
    answers = iter([{**GOOD_DRAFT, "lessons_used": []},
                    {**GOOD_DRAFT, "opening": "We guarantee results for all 420 locations."},
                    GOOD_DRAFT])
    drafted = Drafted(tmp_path, answer=lambda call: next(answers))

    assert len(drafted.prompts) == 3
    assert "guarantee" not in drafted.email["body"].lower()
    assert drafted.email["lessons_used"] == ["lesson:call-1:0"]
