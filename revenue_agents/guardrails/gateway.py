"""Action gateway: the only path from an agent's proposal to a real side effect.

    agent proposes Action -> policy decides -> AUTO:           execute
                                            -> NEEDS_APPROVAL: approval queue -> human -> execute
                                            -> BLOCKED:        nothing happens

Every step is written to the audit log. Executed action ids are remembered,
so a retried event can't send the same email twice (idempotency).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Callable

from revenue_agents.guardrails.approvals import ApprovalQueue, ApprovalStatus
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.policy import Action, ActionType, AutonomyPolicy, Decision
from revenue_agents.models import Deal, Model
from revenue_agents.storage import STATE_DIR, read_json, write_json

Handler = Callable[[Action], Any]


class Outcome(str, Enum):
    EXECUTED = "executed"
    QUEUED = "queued"
    BLOCKED = "blocked"
    DUPLICATE = "duplicate"


class ActionResult(Model):
    action_id: str
    outcome: Outcome
    reason: str
    result: Any = None


class ActionGateway:
    def __init__(
        self,
        policy: AutonomyPolicy,
        approvals: ApprovalQueue,
        audit: AuditLog,
        handlers: dict[ActionType, Handler],
        executed_path: Path = STATE_DIR / "executed_actions.json",
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._handlers = handlers
        self._executed_path = Path(executed_path)

    def submit(self, action: Action, deal: Deal) -> ActionResult:
        """An agent proposes an action. The policy decides what happens to it."""
        if self._already_executed(action.id):
            self._log(action, "duplicate_ignored", "Action already executed")
            return ActionResult(action_id=action.id, outcome=Outcome.DUPLICATE, reason="Action already executed")

        decision = self._policy.decide(action, deal)
        self._log(action, f"policy_{decision.decision.value}", f"[{decision.rule}] {decision.reason}")

        if decision.decision is Decision.AUTO:
            return self._execute(action, decision.reason)
        if decision.decision is Decision.NEEDS_APPROVAL:
            self._approvals.submit(action, decision.reason)
            return ActionResult(action_id=action.id, outcome=Outcome.QUEUED, reason=decision.reason)
        return ActionResult(action_id=action.id, outcome=Outcome.BLOCKED, reason=decision.reason)

    def execute_approved(self, request_id: str, deal: Deal) -> ActionResult:
        """Run an action a human approved. The kill switch and agent scope still apply."""
        request = self._approvals.get(request_id)
        if request.status is not ApprovalStatus.APPROVED:
            raise PermissionError(f"request '{request_id}' is {request.status.value}, not approved")
        if self._already_executed(request.action.id):
            return ActionResult(action_id=request.action.id, outcome=Outcome.DUPLICATE, reason="Action already executed")

        decision = self._policy.decide(request.action, deal)
        if decision.decision is Decision.BLOCKED:
            self._log(request.action, "policy_blocked", f"[{decision.rule}] {decision.reason}")
            return ActionResult(action_id=request.action.id, outcome=Outcome.BLOCKED, reason=decision.reason)

        return self._execute(request.action, f"Approved by {request.decided_by}")

    def _execute(self, action: Action, reason: str) -> ActionResult:
        handler = self._handlers.get(action.type)
        if handler is None:
            self._log(action, "no_handler", f"No handler registered for {action.type.value}")
            return ActionResult(action_id=action.id, outcome=Outcome.BLOCKED, reason="No handler registered")

        result = handler(action)
        self._mark_executed(action.id)
        self._log(action, "executed", reason)
        return ActionResult(action_id=action.id, outcome=Outcome.EXECUTED, reason=reason, result=result)

    def _already_executed(self, action_id: str) -> bool:
        return action_id in read_json(self._executed_path, [])

    def _mark_executed(self, action_id: str) -> None:
        write_json(self._executed_path, [*read_json(self._executed_path, []), action_id])

    def _log(self, action: Action, event: str, detail: str) -> None:
        self._audit.record(action.agent, event, deal_id=action.deal_id, action_id=action.id, detail=detail)
