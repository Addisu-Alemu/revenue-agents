"""Approval queue: high-risk actions wait here until a human approves or rejects them."""

from __future__ import annotations

import datetime as dt
from enum import Enum
from pathlib import Path

from revenue_agents.guardrails.policy import Action
from revenue_agents.models import Model
from revenue_agents.storage import STATE_DIR, locked, read_json, utc_now, write_json


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRequest(Model):
    id: str
    action: Action
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: dt.datetime
    decided_by: str | None = None
    decided_at: dt.datetime | None = None
    note: str | None = None


class ApprovalQueue:
    """File-backed queue. One request per action id, so resubmitting is safe.

    Every read-modify-write holds a file lock, so concurrent submits or
    decisions can't overwrite each other.
    """

    def __init__(self, path: Path = STATE_DIR / "approvals.json") -> None:
        self._path = Path(path)

    def submit(self, action: Action, reason: str) -> ApprovalRequest:
        with locked(self._path):
            requests = self._load()
            if action.id not in requests:
                requests[action.id] = ApprovalRequest(id=action.id, action=action, reason=reason, created_at=utc_now())
                self._save(requests)
            return requests[action.id]

    def get(self, request_id: str) -> ApprovalRequest:
        request = self._load().get(request_id)
        if request is None:
            raise KeyError(f"no approval request '{request_id}'")
        return request

    def pending(self) -> list[ApprovalRequest]:
        return [request for request in self._load().values() if request.status is ApprovalStatus.PENDING]

    def approve(self, request_id: str, reviewer: str, note: str | None = None) -> ApprovalRequest:
        return self._decide(request_id, ApprovalStatus.APPROVED, reviewer, note)

    def reject(self, request_id: str, reviewer: str, note: str | None = None) -> ApprovalRequest:
        return self._decide(request_id, ApprovalStatus.REJECTED, reviewer, note)

    def _decide(self, request_id: str, status: ApprovalStatus, reviewer: str, note: str | None) -> ApprovalRequest:
        with locked(self._path):
            requests = self._load()
            request = requests.get(request_id)
            if request is None:
                raise KeyError(f"no approval request '{request_id}'")
            if request.status is not ApprovalStatus.PENDING:
                raise ValueError(f"request '{request_id}' is already {request.status.value}")

            requests[request_id] = request.model_copy(
                update={"status": status, "decided_by": reviewer, "decided_at": utc_now(), "note": note}
            )
            self._save(requests)
            return requests[request_id]

    def _load(self) -> dict[str, ApprovalRequest]:
        return {key: ApprovalRequest.model_validate(value) for key, value in read_json(self._path, {}).items()}

    def _save(self, requests: dict[str, ApprovalRequest]) -> None:
        write_json(self._path, {key: value.model_dump(mode="json") for key, value in requests.items()})
