"""E-signature: send the IO for signature and check the result (DocuSign in production, mocked here).

The orchestrator never trusts a "signed" webhook on its own: it looks the
envelope up with the provider and checks it was signed, and that it belongs
to the deal waiting for that signature.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from pathlib import Path
from typing import Protocol

from revenue_agents.models import Model
from revenue_agents.storage import STATE_DIR, locked, read_json, utc_now, write_json


class EnvelopeStatus(str, Enum):
    SENT = "sent"
    SIGNED = "signed"


class Envelope(Model):
    id: str
    deal_id: str
    io_number: str
    signer_email: str
    status: EnvelopeStatus = EnvelopeStatus.SENT
    sent_at: dt.datetime
    signed_by: str | None = None
    signed_at: dt.datetime | None = None


class ESignProvider(Protocol):
    def send(self, deal_id: str, io_number: str, document: str, signer_email: str) -> Envelope: ...
    def get(self, envelope_id: str) -> Envelope: ...


class MockESign:
    """A file-backed stand-in for DocuSign. `sign()` simulates the customer signing."""

    def __init__(self, path: Path = STATE_DIR / "esign.json") -> None:
        self._path = Path(path)

    def send(self, deal_id: str, io_number: str, document: str, signer_email: str) -> Envelope:
        """One envelope per IO: sending again returns the same envelope (safe to retry)."""
        envelope_id = f"ENV-{io_number}"
        with locked(self._path):
            envelopes = read_json(self._path, {})
            if envelope_id not in envelopes:
                envelope = Envelope(id=envelope_id, deal_id=deal_id, io_number=io_number, signer_email=signer_email,
                                    sent_at=utc_now())
                envelopes[envelope_id] = envelope.model_dump(mode="json")
                write_json(self._path, envelopes)
            return Envelope.model_validate(envelopes[envelope_id])

    def get(self, envelope_id: str) -> Envelope:
        envelope = read_json(self._path, {}).get(envelope_id)
        if envelope is None:
            raise KeyError(f"no envelope '{envelope_id}'")
        return Envelope.model_validate(envelope)

    def sign(self, envelope_id: str, signed_by: str) -> Envelope:
        with locked(self._path):
            envelopes = read_json(self._path, {})
            if envelope_id not in envelopes:
                raise KeyError(f"no envelope '{envelope_id}'")
            envelope = Envelope.model_validate(envelopes[envelope_id]).model_copy(
                update={"status": EnvelopeStatus.SIGNED, "signed_by": signed_by, "signed_at": utc_now()})
            envelopes[envelope_id] = envelope.model_dump(mode="json")
            write_json(self._path, envelopes)
            return envelope
