"""Outreach and deal desk agents. LLM answers are scripted (FakeLLM)."""

import datetime as dt

import pytest

from revenue_agents.agents.deal_desk import DealDeskAgent
from revenue_agents.agents.outreach import DraftRejectedError, OutreachAgent
from revenue_agents.guardrails.kill_switch import KillSwitch
from revenue_agents.guardrails.policy import ActionType, AutonomyPolicy, Decision
from revenue_agents.llm import FakeLLM
from revenue_agents.models import DealFields, Lesson, Stage, Tier

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


def draft(leads, enricher, segments, make_deal, answer=GOOD_DRAFT, lessons=(BRAND_LESSON,), tier=Tier.A):
    llm = FakeLLM({"OutreachDraft": answer})
    lead = leads["L-001"]
    deal = make_deal(stage=Stage.SCORED, tier=tier)
    return OutreachAgent(llm, segments).run(lead, deal, enricher.enrich(lead), list(lessons)), llm, deal


# --- Outreach ----------------------------------------------------------------------


def test_email_is_built_from_the_segment_template(leads, enricher, segments, make_deal):
    result, _, _ = draft(leads, enricher, segments, make_deal)
    email = result.output

    assert email.to == "maya.thompson@brightbite.example"
    assert email.subject == "Local ads for every BrightBite Burgers location"
    assert email.body.startswith("Hi Maya,")
    assert "60 new franchise locations" in email.body
    assert "Franchisees can only change the fields corporate allows" in email.body  # value prop #1 from brand.yaml
    assert "a 30-minute call next week" in email.body
    assert email.body.rstrip().endswith("Daniel Reyes")


def test_tier_a_first_email_goes_to_approval(leads, enricher, segments, make_deal, policy):
    result, _, deal = draft(leads, enricher, segments, make_deal, tier=Tier.A)
    [action] = result.actions
    assert action.id == "send_email:D-001:first_touch"
    assert policy.decide(action, deal).decision is Decision.NEEDS_APPROVAL


def test_tier_b_first_email_is_automatic(leads, enricher, segments, make_deal, policy):
    result, _, deal = draft(leads, enricher, segments, make_deal, tier=Tier.B)
    assert policy.decide(result.actions[0], deal).decision is Decision.AUTO


def test_lessons_are_offered_and_their_use_is_recorded(leads, enricher, segments, make_deal):
    result, llm, _ = draft(leads, enricher, segments, make_deal, lessons=(BRAND_LESSON, AGENCY_LESSON))
    prompt = llm.calls[0].prompt

    assert "[lesson:call-1:0] Franchise brands care more about brand control" in prompt
    assert "Agency tip." not in prompt  # other segment's lessons are not offered
    assert result.output.lessons_used == ["lesson:call-1:0"]
    assert result.actions[0].payload["lessons_used"] == ["lesson:call-1:0"]


def test_global_lessons_from_another_segment_are_offered(leads, enricher, segments, make_deal):
    shared = AGENCY_LESSON.model_copy(update={"id": "lesson:call-2:g0", "text": "Offer a short pilot.", "scope": "global"})
    answer = {**GOOD_DRAFT, "lessons_used": ["lesson:call-2:g0"]}
    result, llm, _ = draft(leads, enricher, segments, make_deal, answer=answer, lessons=(AGENCY_LESSON, shared))

    prompt = llm.calls[0].prompt
    assert "[lesson:call-2:g0] Offer a short pilot." in prompt
    assert "Agency tip." not in prompt
    assert result.output.lessons_used == ["lesson:call-2:g0"]


def test_model_cannot_cite_lessons_it_was_not_given(leads, enricher, segments, make_deal):
    answer = {**GOOD_DRAFT, "lessons_used": ["lesson:call-1:0", "lesson:made-up"]}
    result, _, _ = draft(leads, enricher, segments, make_deal, answer=answer)
    assert result.output.lessons_used == ["lesson:call-1:0"]


def test_without_lessons_the_prompt_says_none_yet(leads, enricher, segments, make_deal):
    _, llm, _ = draft(leads, enricher, segments, make_deal, lessons=())
    assert "- (none yet)" in llm.calls[0].prompt


def test_enrichment_facts_are_wrapped_as_untrusted(leads, enricher, segments, make_deal):
    _, llm, _ = draft(leads, enricher, segments, make_deal)
    assert '<untrusted source="enrichment:brightbite.example">' in llm.calls[0].prompt


def test_forbidden_phrase_triggers_one_retry_with_feedback(leads, enricher, segments, make_deal):
    answers = iter([{**GOOD_DRAFT, "opening": "We guarantee results for all 420 locations."}, GOOD_DRAFT])
    result, llm, _ = draft(leads, enricher, segments, make_deal, answer=lambda call: next(answers))

    assert len(llm.calls) == 2
    assert "forbidden phrases ['guarantee']" in llm.calls[1].prompt
    assert "guarantee" not in result.output.body.lower()


def test_draft_is_rejected_after_repeated_violations(leads, enricher, segments, make_deal):
    bad = {**GOOD_DRAFT, "opening": "Huge DISCOUNT for early movers!"}
    with pytest.raises(DraftRejectedError, match="forbidden phrases \\['discount'\\]"):
        draft(leads, enricher, segments, make_deal, answer=bad)


def test_invalid_value_prop_choice_is_rejected(leads, enricher, segments, make_deal):
    with pytest.raises(DraftRejectedError, match="value_prop_index must be 0-2"):
        draft(leads, enricher, segments, make_deal, answer={**GOOD_DRAFT, "value_prop_index": 7})


def test_forbidden_phrase_check_matches_whole_words_only(leads, enricher, segments, make_deal):
    answer = {**GOOD_DRAFT, "opening": "Your guaranteed-delivery promise caught our eye."}  # "guaranteed" != "guarantee"
    result, _, _ = draft(leads, enricher, segments, make_deal, answer=answer)
    assert "guaranteed-delivery" in result.output.body


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
def test_a_greeting_from_the_model_is_not_doubled(leads, enricher, segments, make_deal, opening):
    """Found on the first real Claude run: the template greets, so the model's own greeting is removed."""
    result, _, _ = draft(leads, enricher, segments, make_deal, answer={**GOOD_DRAFT, "opening": opening})
    body = result.output.body
    assert body.count("Maya") == 1
    assert "Hi Maya,\n\nCongratulations on the 60 new BrightBite locations" in body


def test_words_that_merely_start_like_a_greeting_are_kept(leads, enricher, segments, make_deal):
    opening = "Highlights from your expansion caught our eye."
    result, _, _ = draft(leads, enricher, segments, make_deal, answer={**GOOD_DRAFT, "opening": opening})
    assert "Highlights from your expansion caught our eye." in result.output.body


def test_prompt_tells_the_model_not_to_greet(leads, enricher, segments, make_deal):
    _, llm, _ = draft(leads, enricher, segments, make_deal)
    assert "Don't start with a greeting" in llm.calls[0].system


def test_prompt_tells_the_model_not_to_repeat_the_value_prop(leads, enricher, segments, make_deal):
    """Found on a real Claude run: the opening restated the value prop that the template prints right after it."""
    _, llm, _ = draft(leads, enricher, segments, make_deal)
    assert "Don't describe what Cool AI does or restate a value" in llm.calls[0].system


# --- Lessons must be visibly used -------------------------------------------------------


def test_a_draft_that_ignores_offered_lessons_is_asked_once_to_apply_one(leads, enricher, segments, make_deal):
    """Found on a real Claude run: offered five lessons, it cited none, so the loop never showed."""
    answers = iter([{**GOOD_DRAFT, "lessons_used": []}, GOOD_DRAFT])
    result, llm, _ = draft(leads, enricher, segments, make_deal, answer=lambda call: next(answers))

    assert len(llm.calls) == 2
    assert "applied none" in llm.calls[1].prompt
    assert result.output.lessons_used == ["lesson:call-1:0"]


def test_ignoring_lessons_twice_still_sends_the_email(leads, enricher, segments, make_deal):
    """The nudge improves the email; it never blocks it."""
    result, llm, _ = draft(leads, enricher, segments, make_deal, answer={**GOOD_DRAFT, "lessons_used": []})

    assert len(llm.calls) == 2
    assert result.output.lessons_used == []
    assert result.actions[0].type is ActionType.SEND_EMAIL


def test_no_lesson_nudge_when_no_lessons_were_offered(leads, enricher, segments, make_deal):
    _, llm, _ = draft(leads, enricher, segments, make_deal, answer={**GOOD_DRAFT, "lessons_used": []}, lessons=())
    assert len(llm.calls) == 1


def test_lesson_nudge_does_not_use_up_the_forbidden_phrase_retry(leads, enricher, segments, make_deal):
    answers = iter([{**GOOD_DRAFT, "lessons_used": []},
                    {**GOOD_DRAFT, "opening": "We guarantee results for all 420 locations."},
                    GOOD_DRAFT])
    result, llm, _ = draft(leads, enricher, segments, make_deal, answer=lambda call: next(answers))

    assert len(llm.calls) == 3
    assert "guarantee" not in result.output.body.lower()
    assert result.output.lessons_used == ["lesson:call-1:0"]
