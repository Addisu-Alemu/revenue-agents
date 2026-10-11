"""Stall detection, reminders and the daily digest, driven by scheduled events."""

import datetime as dt
import json

import pytest
import yaml

from revenue_agents.demo_llm import demo_llm
from revenue_agents.events import Event, EventType
from revenue_agents.models import Stage
from revenue_agents.segments import SEGMENTS_DIR, SegmentConfigError, SegmentRegistry, TrackingConfig
from revenue_agents.storage import utc_now
from revenue_agents.system import build_system

NOW = dt.datetime(2026, 10, 20, 9, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def system(tmp_path):
    return build_system(demo_llm(), state_dir=tmp_path)


def put_in_stage(system, lead_id, stage, days_ago):
    """Create the lead's deal and pretend it entered `stage` some days before NOW."""
    deal = system.crm.deal_for_lead(lead_id)
    system.crm.save_deal(deal.model_copy(update={"stage": stage, "stage_entered_at": NOW - dt.timedelta(days=days_ago)}))
    return deal.id


def scheduled(event_type, day=NOW):
    prefix = "sla" if event_type is EventType.SLA_CHECK else "digest"
    return Event(id=f"{prefix}:{day.date()}", type=event_type, payload={"now": day.isoformat()})


# --- Stall detection -------------------------------------------------------------------


def test_deal_over_its_stage_limit_is_stalled(system):
    put_in_stage(system, "L-002", Stage.REPLIED, days_ago=4)  # brand limit for replied: 3
    put_in_stage(system, "L-001", Stage.MEETING, days_ago=4)  # brand limit for meeting: 7

    [stalled] = system.orchestrator._tracker.stalled(NOW)
    assert (stalled.deal_id, stalled.stage, stalled.limit_days) == ("D-002", Stage.REPLIED, 3)
    assert stalled.contact == "Owen Castillo"


def test_closed_and_nurtured_deals_never_stall(system):
    system.orchestrator.handle(Event(id="lead:L-003", type=EventType.LEAD_CREATED, payload={"lead_id": "L-003"}))
    put_in_stage(system, "L-001", Stage.HANDOFF, days_ago=60)
    put_in_stage(system, "L-002", Stage.CLOSED_LOST, days_ago=60)
    later = utc_now() + dt.timedelta(days=30)  # Pebble & Pine (Tier C) has sat in "scored" for a month

    assert system.orchestrator._tracker.stalled(later) == []


def test_sla_check_flags_and_reminds_once(system):
    put_in_stage(system, "L-002", Stage.REPLIED, days_ago=4)

    first = system.orchestrator.handle(scheduled(EventType.SLA_CHECK))
    assert "D-002 Harbor Lane Home: Follow up with Owen Castillo: no progress in replied for 4 days (limit 3)" in first.steps
    assert "stalled_replied" in system.crm.get_deal("D-002").flags

    next_day = system.orchestrator.handle(scheduled(EventType.SLA_CHECK, NOW + dt.timedelta(days=1)))
    assert next_day.steps[-1] == "D-002 Harbor Lane Home: still stalled (duplicate)"
    assert [a.kind for a in system.crm.timeline("D-002")].count("reminder") == 1


def test_same_day_schedule_firing_twice_runs_once(system):
    system.orchestrator.handle(scheduled(EventType.SLA_CHECK))
    assert system.orchestrator.handle(scheduled(EventType.SLA_CHECK)).status == "duplicate"


# --- Digest ------------------------------------------------------------------------------


def test_digest_summarises_what_needs_a_human(tmp_path, system):
    system.orchestrator.handle(Event(id="lead:L-001", type=EventType.LEAD_CREATED, payload={"lead_id": "L-001"}))
    digest_time = utc_now() + dt.timedelta(hours=30)  # Maya's approval has waited 30 hours
    deal = system.crm.deal_for_lead("L-002")
    system.crm.save_deal(deal.model_copy(update={
        "stage": Stage.REPLIED, "stage_entered_at": digest_time - dt.timedelta(days=4),
        "flags": ["meeting_requested", "pricing_requested"]}))
    (tmp_path / "claims").mkdir(exist_ok=True)
    (tmp_path / "claims" / "send_email%3AD-009%3Afirst_touch.json").write_text(
        '{"action_id": "send_email:D-009:first_touch", "status": "in_progress"}')

    report = system.orchestrator.handle(scheduled(EventType.DIGEST_DAILY, digest_time))
    text = "\n".join(report.steps)

    assert "Pipeline: scored 1, replied 1" in text
    assert "First email to Maya Thompson at BrightBite Burgers (30h, OVERDUE)" in text
    assert "D-002 Harbor Lane Home: replied for" in text
    assert "D-002: pricing_requested" in text
    assert "meeting_requested" not in text  # informational, not "needs attention"
    assert "send_email:D-009:first_touch (started but never finished" in text


def test_digest_lists_todays_lessons_and_is_saved(tmp_path, system, transcript):
    system.orchestrator.handle(Event(id="lead:L-001", type=EventType.LEAD_CREATED, payload={"lead_id": "L-001"}))
    system.orchestrator.handle(Event(id="call:1", type=EventType.CALL_COMPLETED, payload={
        "lead_id": "L-001", "transcript_id": "L-001_discovery_call", "transcript": transcript}))
    today = utc_now()

    report = system.orchestrator.handle(scheduled(EventType.DIGEST_DAILY, today))

    assert "New lessons (2):" in report.steps
    saved = json.loads((tmp_path / "digests" / f"{today.date()}.json").read_text())
    assert saved["pipeline"] == {"proposal": 1}


def test_empty_digest_says_none(system):
    report = system.orchestrator.handle(scheduled(EventType.DIGEST_DAILY))
    assert report.steps[1] == "Pipeline: empty"
    assert report.steps.count("  none") == 7


# --- Config ------------------------------------------------------------------------------


def test_pack_overrides_only_the_stages_it_lists():
    config = TrackingConfig(stall_days={Stage.REPLIED: 10})
    assert config.limit_for(Stage.REPLIED) == 10
    assert config.limit_for(Stage.MEETING) == 7  # default
    assert config.limit_for(Stage.HANDOFF) is None


@pytest.mark.parametrize(("stall_days", "error"), [({"handoff": 5}, "closed stages can't stall"),
                                                   ({"replied": 0}, "must be positive")])
def test_invalid_tracking_config_is_rejected(tmp_path, stall_days, error):
    config = yaml.safe_load((SEGMENTS_DIR / "brand.yaml").read_text())
    config["tracking"] = {"stall_days": stall_days}
    (tmp_path / "brand.yaml").write_text(yaml.safe_dump(config))
    with pytest.raises(SegmentConfigError, match=error):
        SegmentRegistry(tmp_path).get("brand")


def test_event_time_defaults_to_arrival():
    event = Event(id="x", type=EventType.SLA_CHECK)
    assert event.now == event.received_at
