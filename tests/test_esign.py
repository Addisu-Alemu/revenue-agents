"""E-signature: IOs go out as envelopes; only a verified, matching signature closes the deal."""

import pytest

from revenue_agents.demo_llm import demo_llm
from revenue_agents.esign import EnvelopeStatus, MockESign
from revenue_agents.events import Event, EventType
from revenue_agents.models import Stage
from revenue_agents.system import build_system


@pytest.fixture
def system(tmp_path):
    return build_system(demo_llm(), state_dir=tmp_path)


def event(type_, event_id, **payload):
    return Event(id=event_id, type=type_, payload=payload)


def deal_awaiting_signature(system, transcript):
    """Run BrightBite to the signature stage: lead, call, IO approved and sent."""
    handle = system.orchestrator.handle
    handle(event(EventType.LEAD_CREATED, "lead:L-001", lead_id="L-001"))
    handle(event(EventType.CALL_COMPLETED, "call:1", lead_id="L-001", transcript_id="L-001_discovery_call",
                 transcript=transcript))
    handle(event(EventType.APPROVAL_DECIDED, "approve:io", request_id="send_io:D-001:IO-D-001", approved=True,
                 reviewer="maria@cool.example"))
    return system.crm.get_deal("D-001")


def signed(envelope_id="ENV-IO-D-001", event_id="esign:1"):
    return event(EventType.SIGNATURE_COMPLETED, event_id, envelope_id=envelope_id)


# --- Mock provider --------------------------------------------------------------------


def test_one_envelope_per_io(tmp_path):
    esign = MockESign(tmp_path / "esign.json")
    first = esign.send("D-001", "IO-D-001", "doc", "maya@x")
    again = esign.send("D-001", "IO-D-001", "doc", "maya@x")
    assert first == again
    assert first.status is EnvelopeStatus.SENT


def test_signing_records_who_and_when(tmp_path):
    esign = MockESign(tmp_path / "esign.json")
    esign.send("D-001", "IO-D-001", "doc", "maya@x")
    envelope = esign.sign("ENV-IO-D-001", signed_by="Ken Adeyemi")
    assert envelope.status is EnvelopeStatus.SIGNED
    assert envelope.signed_by == "Ken Adeyemi"
    assert MockESign(tmp_path / "esign.json").get("ENV-IO-D-001").signed_at is not None


@pytest.mark.parametrize("call", [lambda e: e.get("ENV-NOPE"), lambda e: e.sign("ENV-NOPE", "x")])
def test_unknown_envelope_raises(tmp_path, call):
    with pytest.raises(KeyError):
        call(MockESign(tmp_path / "esign.json"))


# --- Signature flow --------------------------------------------------------------------


def test_approved_io_is_sent_as_an_envelope(system, transcript):
    deal = deal_awaiting_signature(system, transcript)
    assert deal.stage is Stage.SIGNATURE
    assert system.esign.get("ENV-IO-D-001").signer_email == "maya.thompson@brightbite.example"


def test_verified_signature_closes_the_deal_and_hands_off(system, transcript):
    deal_awaiting_signature(system, transcript)
    system.esign.sign("ENV-IO-D-001", signed_by="Ken Adeyemi")

    report = system.orchestrator.handle(signed())

    assert system.crm.get_deal("D-001").stage is Stage.HANDOFF
    assert "IO-D-001 signed by Ken Adeyemi (verified with the e-sign provider)" in report.steps
    [handoff] = [a for a in system.crm.timeline("D-001") if a.kind == "handoff"]
    assert handoff.data["budget_usd"] == 250000
    assert handoff.data["signed_by"] == "Ken Adeyemi"
    assert handoff.data["flight_start"] == "2027-01-15"


def test_forged_webhook_for_unknown_envelope_is_rejected(system, transcript):
    deal_awaiting_signature(system, transcript)
    report = system.orchestrator.handle(signed("ENV-FAKE-001"))

    assert report.steps == ["Rejected: unknown envelope 'ENV-FAKE-001' (forged or misrouted webhook)"]
    assert system.crm.get_deal("D-001").stage is Stage.SIGNATURE
    assert system.audit.events()[-1].event == "signature_rejected"


def test_webhook_before_the_provider_confirms_is_not_trusted(system, transcript):
    deal_awaiting_signature(system, transcript)  # envelope sent, but nobody signed it
    system.orchestrator.handle(signed())

    deal = system.crm.get_deal("D-001")
    assert deal.stage is Stage.SIGNATURE
    assert "signature_unconfirmed" in deal.flags


def test_signature_for_a_deal_not_waiting_for_one_is_flagged(system):
    system.orchestrator.handle(event(EventType.LEAD_CREATED, "lead:L-001", lead_id="L-001"))
    system.esign.send("D-001", "IO-D-001", "doc", "maya@x")  # an envelope exists, but the deal is only "scored"
    system.esign.sign("ENV-IO-D-001", signed_by="Ken Adeyemi")

    system.orchestrator.handle(signed())

    deal = system.crm.get_deal("D-001")
    assert deal.stage is Stage.SCORED
    assert "signature_mismatch" in deal.flags


def test_duplicate_signature_webhook_closes_once(system, transcript):
    deal_awaiting_signature(system, transcript)
    system.esign.sign("ENV-IO-D-001", signed_by="Ken Adeyemi")
    system.orchestrator.handle(signed())

    assert system.orchestrator.handle(signed()).status == "duplicate"
    assert [a.kind for a in system.crm.timeline("D-001")].count("handoff") == 1
