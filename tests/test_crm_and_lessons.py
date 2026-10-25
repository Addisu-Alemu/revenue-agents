"""Mock HubSpot and the lesson store persist state safely."""

import datetime as dt
import threading

import pytest

from revenue_agents.crm import MockHubSpot
from revenue_agents.lessons import LessonStore
from revenue_agents.models import Lead, Lesson, Stage


@pytest.fixture
def crm(tmp_path):
    return MockHubSpot(tmp_path / "hubspot.json")


@pytest.fixture
def store(tmp_path):
    return LessonStore(tmp_path / "lessons.json")


def lesson(lesson_id, segment="brand", day=1, applies_to=("outreach", "call_brief"), scope="segment"):
    return Lesson(id=lesson_id, segment=segment, source="call", source_id="call-1", text=f"Lesson {lesson_id}",
                  scope=scope,
                  applies_to=list(applies_to), created_at=dt.datetime(2026, 10, day, tzinfo=dt.timezone.utc))


# --- Mock HubSpot --------------------------------------------------------------------


def test_seeded_with_mock_leads(crm):
    assert len(crm.leads()) == 8
    assert crm.get_lead("L-001").company == "BrightBite Burgers"


def test_deal_is_created_once_at_prospect_stage(crm):
    deal = crm.deal_for_lead("L-001")
    assert deal.id == "D-001"
    assert deal.stage is Stage.PROSPECT
    assert deal.stage_entered_at is not None
    assert crm.deal_for_lead("L-001") == deal  # second call returns the same deal
    assert len(crm.deals()) == 1


def test_saved_deal_survives_restart(tmp_path, crm):
    deal = crm.deal_for_lead("L-001")
    crm.save_deal(deal.model_copy(update={"stage": Stage.SCORED, "flags": ["hot"]}))

    reloaded = MockHubSpot(tmp_path / "hubspot.json").get_deal("D-001")
    assert reloaded.stage is Stage.SCORED
    assert reloaded.flags == ["hot"]


def test_timeline_keeps_order_per_deal(crm):
    crm.deal_for_lead("L-001")
    crm.deal_for_lead("L-002")
    crm.log("D-001", "note", "first")
    crm.log("D-002", "note", "other deal")
    crm.log("D-001", "email_sent", "second", {"to": "maya@x"})

    timeline = crm.timeline("D-001")
    assert [a.summary for a in timeline] == ["first", "second"]
    assert timeline[1].data == {"to": "maya@x"}


def test_new_inbound_lead_can_be_added(crm):
    new = crm.get_lead("L-001").model_copy(update={"id": "L-099", "company": "New Co"})
    crm.add_lead(new)
    crm.add_lead(new)  # adding twice is harmless
    assert crm.get_lead("L-099").company == "New Co"
    assert len(crm.leads()) == 9


@pytest.mark.parametrize("call", [lambda c: c.get_lead("L-999"), lambda c: c.get_deal("D-999"),
                                  lambda c: c.deal_for_lead("L-999")])
def test_unknown_records_raise(crm, call):
    with pytest.raises(KeyError):
        call(crm)


def test_saving_an_unknown_deal_raises(crm):
    deal = crm.deal_for_lead("L-001").model_copy(update={"id": "D-999"})
    with pytest.raises(KeyError):
        crm.save_deal(deal)


def test_concurrent_timeline_writes_are_not_lost(crm):
    crm.deal_for_lead("L-001")
    barrier = threading.Barrier(15)

    def write(i):
        barrier.wait()
        crm.log("D-001", "note", f"entry {i}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(15)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    timeline = crm.timeline("D-001")
    assert len(timeline) == 15
    assert len({a.id for a in timeline}) == 15


def test_lead_model_round_trips(crm):
    assert isinstance(crm.leads()[0], Lead)


# --- Lesson store ----------------------------------------------------------------------


def test_lessons_are_saved_once(store):
    assert store.add(lesson("a")) is True
    assert store.add(lesson("a")) is False
    assert len(store.all()) == 1


def test_for_segment_filters_and_sorts_newest_first(store):
    store.add(lesson("old", day=1))
    store.add(lesson("new", day=5))
    store.add(lesson("agency", segment="agency", day=9))
    store.add(lesson("brief-only", day=7, applies_to=("call_brief",)))

    assert [l.id for l in store.for_segment("brand")] == ["new", "old"]
    assert [l.id for l in store.for_segment("brand", purpose="call_brief")] == ["brief-only", "new", "old"]
    assert [l.id for l in store.for_segment("brand", limit=1)] == ["new"]


def test_global_lessons_reach_every_segment(store):
    store.add(lesson("brand-only", day=1))
    store.add(lesson("from-brand", day=2, scope="global"))
    store.add(lesson("from-agency", segment="agency", day=3, scope="global"))

    assert [l.id for l in store.for_segment("agency")] == ["from-agency", "from-brand"]
    assert [l.id for l in store.for_segment("brand")] == ["from-agency", "from-brand", "brand-only"]
    assert [l.id for l in store.for_segment("publisher")] == ["from-agency", "from-brand"]  # even a new segment


def test_lesson_uses_are_traceable(tmp_path, store):
    store.add(lesson("a"))
    store.record_use(["a"], used_in="send_email:D-002:first_touch", deal_id="D-002")
    store.record_use([], used_in="send_email:D-003:first_touch", deal_id="D-003")  # nothing to record

    uses = LessonStore(tmp_path / "lessons.json").uses("a")
    assert [(u.used_in, u.deal_id) for u in uses] == [("send_email:D-002:first_touch", "D-002")]
    assert len(store.uses()) == 1
