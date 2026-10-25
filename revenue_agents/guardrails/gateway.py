"""Action gateway: the only path from an agent's proposal to a real side effect.

    agent proposes Action -> policy decides -> AUTO:           execute
                                            -> NEEDS_APPROVAL: approval queue -> human -> execute
                                            -> BLOCKED:        nothing happens

Every step is written to the audit log. Before an action executes, its id is
claimed atomically (see claims.py), so a retried or duplicated event can't
run the same action twice, even when copies arrive at the same moment.

Delivery is at-most-once: if the process crashes mid-handler, the claim stays
"in progress" and the action is not retried automatically. For emails and
contracts, "maybe not sent" (a human can resend) is safer than "sent twice".
Stuck claims are listed by ActionClaims.in_progress() for review.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from revenue_agents.guardrails.approvals import ApprovalQueue, ApprovalStatus
from revenue_agents.guardrails.audit import AuditLog
from revenue_agents.guardrails.claims import ActionClaims
from revenue_agents.guardrails.policy import Action, ActionType, AutonomyPolicy, Decision, PolicyDecision
from revenue_agents.models import Deal, Model

if TYPE_CHECKING:
    from revenue_agents.tracing import Tracer

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
        claims: ActionClaims | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._handlers = handlers
        self._claims = claims or ActionClaims()
        self._tracer = tracer

    def check(self, action: Action, deal: Deal) -> PolicyDecision | None:
        """What the policy would decide, without doing or recording anything. None: the action already ran.

        The agents' PreToolUse hook asks this to allow, defer or deny a tool call; submit() still decides
        again before anything runs.
        """
        if self._claims.exists(action.id):
            return None
        return self._policy.decide(action, deal)

    def submit(self, action: Action, deal: Deal) -> ActionResult:
        """An agent proposes an action. The policy decides what happens to it."""
        if self._claims.exists(action.id):
            return self._duplicate(action)

        decision = self._policy.decide(action, deal)
        self._log(action, f"policy_{decision.decision.value}", f"[{decision.rule}] {decision.reason}")
        self._trace("policy", action, decision=decision.decision.value, rule=decision.rule, reason=decision.reason)

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
        if self._claims.exists(request.action.id):
            return self._duplicate(request.action)

        decision = self._policy.decide(request.action, deal)
        self._trace("policy", request.action, decision=f"{decision.decision.value} (re-check before running an approved "
                    f"action)", rule=decision.rule, reason=decision.reason)
        if decision.decision is Decision.BLOCKED:
            self._log(request.action, "policy_blocked", f"[{decision.rule}] {decision.reason}")
            return ActionResult(action_id=request.action.id, outcome=Outcome.BLOCKED, reason=decision.reason)

        return self._execute(request.action, f"Approved by {request.decided_by}")

    def _execute(self, action: Action, reason: str) -> ActionResult:
        handler = self._handlers.get(action.type)
        if handler is None:
            self._log(action, "no_handler", f"No handler registered for {action.type.value}")
            return ActionResult(action_id=action.id, outcome=Outcome.BLOCKED, reason="No handler registered")

        # The atomic claim is what guarantees "once": the exists() checks above are only a shortcut.
        if not self._claims.claim(action.id):
            return self._duplicate(action)

        try:
            result = handler(action)
        except Exception as error:
            self._claims.release(action.id)
            self._log(action, "failed", f"{type(error).__name__}: {error}")
            raise

        self._claims.complete(action.id)
        self._log(action, "executed", reason)
        self._trace("action", action, outcome="executed", reason=reason)
        return ActionResult(action_id=action.id, outcome=Outcome.EXECUTED, reason=reason, result=result)

    def _duplicate(self, action: Action) -> ActionResult:
        self._log(action, "duplicate_ignored", "Action already executed or in progress")
        return ActionResult(action_id=action.id, outcome=Outcome.DUPLICATE, reason="Action already executed or in progress")

    def _trace(self, kind: str, action: Action, **data: Any) -> None:
        if self._tracer is not None:
            self._tracer.record(kind, action.type.value, {"agent": action.agent, "action_id": action.id, **data})

    def _log(self, action: Action, event: str, detail: str) -> None:
        self._audit.record(action.agent, event, deal_id=action.deal_id, action_id=action.id, detail=detail)
