"""Generic approval state machine for future public or privileged actions.

The service is application code, not a model tool: callers supply a trusted
actor permission set, and every transition is rechecked against the stored
request. Action content is hashed and never stored. Execution is claimed before
any future side effect, so a changed target or content cannot reuse an old
approval. No executor or public-action command is implemented in this phase.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..db.repository import Repository
from ..errors import (
    ApprovalBindingError,
    ApprovalConflictError,
    ApprovalDeniedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalSelfApprovalError,
)

MAX_CONTENT_CHARS = 100_000
MAX_APPROVAL_TTL_S = 7 * 24 * 3600
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return _as_utc(moment).isoformat(timespec="seconds")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ActorKind(str, Enum):
    agent = "agent"
    human = "human"
    system = "system"


class ApprovalPermission(str, Enum):
    propose = "propose"
    decide = "decide"
    cancel = "cancel"
    execute = "execute"


class ApprovalState(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    cancelled = "cancelled"
    executing = "executing"
    executed = "executed"
    failed = "failed"


class ExecutionState(str, Enum):
    executing = "executing"
    executed = "executed"
    failed = "failed"


class ExecutionOutcome(str, Enum):
    executed = "executed"
    failed = "failed"


class ApprovalActor(BaseModel):
    """Trusted caller identity supplied by an application adapter.

    ``permissions`` must come from existing backend checks, never from model
    output or user-provided text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    actor_id: str = Field(min_length=1, max_length=64)
    guild_id: int = Field(gt=0)
    kind: ActorKind
    permissions: frozenset[ApprovalPermission] = Field(default_factory=frozenset)


class ActionBinding(BaseModel):
    """The exact action, target and content an approval is bound to."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    guild_id: int = Field(gt=0)
    target_type: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_.-]{0,31}$")
    target_id: str = Field(min_length=1, max_length=200)
    content_version: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class ActionRequest(ActionBinding):
    """An action request with a bounded expiry."""

    expires_in_s: int = Field(default=900, ge=1, le=MAX_APPROVAL_TTL_S)


class ExecutionRequest(BaseModel):
    """A request to claim and start one approved action."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_id: int = Field(gt=0)
    binding: ActionBinding
    idempotency_key: str = Field(min_length=8, max_length=128)

    def model_post_init(self, __context: Any) -> None:
        if not _IDEMPOTENCY_RE.fullmatch(self.idempotency_key):
            raise ValueError("idempotency_key must match ^[A-Za-z0-9_.:-]{8,128}$")


class ApprovalRequest(BaseModel):
    """Stored approval metadata; the action content itself is never stored."""

    model_config = ConfigDict(extra="forbid")

    id: int
    action_type: str
    guild_id: int
    target_type: str
    target_id: str
    content_version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_by: str
    requester_kind: ActorKind
    state: ApprovalState
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None


class ExecutionAttempt(BaseModel):
    """One claimed execution attempt and its terminal outcome."""

    model_config = ConfigDict(extra="forbid")

    id: int
    approval_id: int
    idempotency_key: str
    executor_id: str
    state: ExecutionState
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    result_ref: str | None = None


class ApprovalService:
    """Backend-enforced approval and execution-claim service."""

    def __init__(self, repo: Repository, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._repo = repo
        self._clock = clock

    # ---------------------------------------------------------------- requests

    async def request_action(self, actor: ApprovalActor, request: ActionRequest) -> ApprovalRequest:
        """Create a pending request from a trusted actor."""
        if actor.guild_id != request.guild_id:
            raise ApprovalDeniedError()
        if ApprovalPermission.propose not in actor.permissions:
            raise ApprovalDeniedError()

        now = _as_utc(self._clock())
        expires_at = now + timedelta(seconds=request.expires_in_s)
        approval_id = await self._repo.insert_approval_request(
            action_type=request.action_type,
            guild_id=str(request.guild_id),
            target_type=request.target_type,
            target_id=request.target_id,
            content_version=request.content_version,
            content_hash=_content_hash(request.content),
            requested_by=actor.actor_id,
            requester_kind=actor.kind.value,
            created_at=_iso(now),
            expires_at=_iso(expires_at),
        )
        return await self.get_request(approval_id, actor=actor)

    async def get_request(self, approval_id: int, *, actor: ApprovalActor) -> ApprovalRequest:
        row = await self._repo.get_approval_request(approval_id, guild_id=str(actor.guild_id))
        if row is None:
            raise ApprovalNotFoundError()
        return self._request_from_row(row)

    # --------------------------------------------------------------- decisions

    async def approve(
        self,
        approval_id: int,
        *,
        actor: ApprovalActor,
        note: str | None = None,
    ) -> ApprovalRequest:
        return await self._decide(approval_id, actor=actor, state=ApprovalState.approved, note=note)

    async def reject(
        self,
        approval_id: int,
        *,
        actor: ApprovalActor,
        note: str | None = None,
    ) -> ApprovalRequest:
        return await self._decide(approval_id, actor=actor, state=ApprovalState.rejected, note=note)

    async def _decide(
        self,
        approval_id: int,
        *,
        actor: ApprovalActor,
        state: ApprovalState,
        note: str | None,
    ) -> ApprovalRequest:
        if actor.kind is not ActorKind.human or ApprovalPermission.decide not in actor.permissions:
            await self._audit_denied(actor, approval_id, "decision", "permission")
            raise ApprovalDeniedError()

        now = _as_utc(self._clock())
        now_iso = _iso(now)
        row = await self._repo.get_approval_request(approval_id, guild_id=str(actor.guild_id))
        if row is None:
            await self._audit_denied(actor, approval_id, "decision", "not_found")
            raise ApprovalNotFoundError()

        if state is ApprovalState.approved and row["requested_by"] == actor.actor_id:
            await self._audit_denied(actor, approval_id, "decision", "self_approval")
            raise ApprovalSelfApprovalError()

        if self._is_expired(row, now_iso):
            await self._expire_row(row, now_iso)
            raise ApprovalExpiredError()
        if row["state"] == ApprovalState.expired.value:
            raise ApprovalExpiredError()
        if row["state"] != ApprovalState.pending.value:
            raise ApprovalConflictError()

        applied = await self._repo.decide_approval_request(
            approval_id,
            guild_id=str(actor.guild_id),
            state=state.value,
            decided_by=actor.actor_id,
            decided_at=now_iso,
            note=note,
            now=now_iso,
        )
        if not applied:
            await self._raise_after_failed_transition(approval_id, actor, now_iso)
        return await self.get_request(approval_id, actor=actor)

    async def cancel(
        self,
        approval_id: int,
        *,
        actor: ApprovalActor,
        reason: str | None = None,
    ) -> ApprovalRequest:
        if ApprovalPermission.cancel not in actor.permissions:
            await self._audit_denied(actor, approval_id, "cancellation", "permission")
            raise ApprovalDeniedError()

        now_iso = _iso(_as_utc(self._clock()))
        row = await self._repo.get_approval_request(approval_id, guild_id=str(actor.guild_id))
        if row is None:
            await self._audit_denied(actor, approval_id, "cancellation", "not_found")
            raise ApprovalNotFoundError()
        if self._is_expired(row, now_iso):
            await self._expire_row(row, now_iso)
            raise ApprovalExpiredError()
        if row["state"] == ApprovalState.expired.value:
            raise ApprovalExpiredError()
        if row["state"] not in (ApprovalState.pending.value, ApprovalState.approved.value):
            raise ApprovalConflictError()

        applied = await self._repo.cancel_approval_request(
            approval_id,
            guild_id=str(actor.guild_id),
            cancelled_by=actor.actor_id,
            cancelled_at=now_iso,
            reason=reason,
            now=now_iso,
        )
        if not applied:
            await self._raise_after_failed_transition(approval_id, actor, now_iso)
        return await self.get_request(approval_id, actor=actor)

    # ------------------------------------------------------------------ expiry

    async def expire_due(self) -> int:
        """Mark all live requests whose expiry passed; returns rows changed."""
        now = _iso(_as_utc(self._clock()))
        rows = await self._repo.approval_requests_due(now)
        expired = 0
        for row in rows:
            if await self._repo.expire_approval_request(int(row["id"]), guild_id=str(row["guild_id"]), now=now):
                expired += 1
        return expired

    async def _expire_row(self, row: Any, now_iso: str) -> None:
        await self._repo.expire_approval_request(int(row["id"]), guild_id=str(row["guild_id"]), now=now_iso)

    # --------------------------------------------------------------- execution

    async def begin_execution(self, request: ExecutionRequest, *, actor: ApprovalActor) -> ExecutionAttempt:
        """Claim one approved attempt, revalidating the binding immediately.

        A future executor must call this method immediately before any side
        effect and must use the returned attempt. Duplicate keys return the
        existing attempt without invoking the side effect again.
        """
        attempt, _created = await self.begin_execution_claim(request, actor=actor)
        return attempt

    async def begin_execution_claim(
        self, request: ExecutionRequest, *, actor: ApprovalActor
    ) -> tuple[ExecutionAttempt, bool]:
        """Like :meth:`begin_execution`, but report whether the claim is new.

        Executors must only perform their side effect when ``created`` is True.
        A replay returns ``created=False`` even while the original attempt is
        still ``executing``, which prevents an ambiguous or duplicate send.
        """
        if ApprovalPermission.execute not in actor.permissions:
            await self._audit_denied(actor, request.approval_id, "execution", "permission")
            raise ApprovalDeniedError()

        now_iso = _iso(_as_utc(self._clock()))
        row = await self._repo.get_approval_request(request.approval_id, guild_id=str(actor.guild_id))
        if row is None:
            await self._audit_denied(actor, request.approval_id, "execution", "not_found")
            raise ApprovalNotFoundError()

        existing = await self._repo.get_approval_execution_by_key(request.idempotency_key)
        if existing is not None:
            if int(existing["approval_id"]) != request.approval_id or existing["executor_id"] != actor.actor_id:
                await self._audit_denied(actor, request.approval_id, "execution", "idempotency_conflict")
                raise ApprovalConflictError()
            await self._validate_binding(row, request, actor)
            return self._execution_from_row(existing), False

        if int(row["guild_id"]) != request.binding.guild_id:
            await self._audit_denied(actor, request.approval_id, "execution", "guild_scope")
            raise ApprovalBindingError()
        if self._is_expired(row, now_iso):
            await self._expire_row(row, now_iso)
            raise ApprovalExpiredError()
        if row["state"] == ApprovalState.expired.value:
            raise ApprovalExpiredError()
        if row["state"] != ApprovalState.approved.value:
            await self._audit_denied(actor, request.approval_id, "execution", "state")
            raise ApprovalConflictError()
        await self._validate_binding(row, request, actor)

        execution_id = await self._repo.claim_approval_execution(
            request.approval_id,
            guild_id=str(actor.guild_id),
            executor_id=actor.actor_id,
            idempotency_key=request.idempotency_key,
            now=now_iso,
            action_type=request.binding.action_type,
            target_type=request.binding.target_type,
            target_id=request.binding.target_id,
            content_version=request.binding.content_version,
            content_hash=_content_hash(request.binding.content),
        )
        if execution_id is None:
            current = await self._repo.get_approval_request(request.approval_id, guild_id=str(actor.guild_id))
            if current is not None and self._is_expired(current, now_iso):
                await self._expire_row(current, now_iso)
                raise ApprovalExpiredError()
            existing = await self._repo.get_approval_execution_by_key(request.idempotency_key)
            if existing is not None and (
                int(existing["approval_id"]) == request.approval_id and existing["executor_id"] == actor.actor_id
            ):
                return self._execution_from_row(existing), False
            raise ApprovalConflictError()

        execution = await self._repo.get_approval_execution(execution_id, guild_id=str(actor.guild_id))
        if execution is None:  # pragma: no cover - storage invariant
            raise ApprovalConflictError()
        return self._execution_from_row(execution), True

    async def finish_execution(
        self,
        execution_id: int,
        *,
        actor: ApprovalActor,
        outcome: ExecutionOutcome,
        error_code: str | None = None,
        result_ref: str | None = None,
    ) -> ExecutionAttempt:
        """Record a terminal outcome for an attempt owned by ``actor``."""
        if ApprovalPermission.execute not in actor.permissions:
            await self._audit_denied(actor, execution_id, "execution", "permission", target_type="execution")
            raise ApprovalDeniedError()
        if error_code is not None and not _ERROR_CODE_RE.fullmatch(error_code):
            raise ValueError("error_code must be a short machine-readable code")
        if result_ref is not None and not result_ref.strip():
            raise ValueError("result_ref must not be blank")

        execution = await self._repo.get_approval_execution(execution_id, guild_id=str(actor.guild_id))
        if execution is None:
            await self._audit_denied(actor, execution_id, "execution", "not_found", target_type="execution")
            raise ApprovalNotFoundError()
        approval = await self._repo.get_approval_request(int(execution["approval_id"]), guild_id=str(actor.guild_id))
        if approval is None:
            await self._audit_denied(actor, execution_id, "execution", "not_found", target_type="execution")
            raise ApprovalNotFoundError()
        if execution["executor_id"] != actor.actor_id:
            await self._audit_denied(actor, execution_id, "execution", "executor_mismatch", target_type="execution")
            raise ApprovalDeniedError()
        if execution["state"] == outcome.value:
            return self._execution_from_row(execution)
        if execution["state"] != ExecutionState.executing.value:
            raise ApprovalConflictError()

        now_iso = _iso(_as_utc(self._clock()))
        applied = await self._repo.finish_approval_execution(
            execution_id,
            approval_id=int(approval["id"]),
            guild_id=str(actor.guild_id),
            executor_id=actor.actor_id,
            outcome=outcome.value,
            finished_at=now_iso,
            error_code=error_code,
            result_ref=result_ref,
        )
        current = await self._repo.get_approval_execution(execution_id, guild_id=str(actor.guild_id))
        if current is None:  # pragma: no cover - storage invariant
            raise ApprovalConflictError()
        if not applied and current["state"] != outcome.value:
            raise ApprovalConflictError()
        return self._execution_from_row(current)

    async def reconcile_execution(
        self,
        execution_id: int,
        *,
        actor: ApprovalActor,
        result_ref: str,
    ) -> ExecutionAttempt:
        """Record evidence that a stuck execution's message exists.

        This is the only non-owner success path. It is available to a trusted
        human with execute permission, requires the claim and parent request to
        still be ``executing``, and never retries or resets anything.
        """
        if ApprovalPermission.execute not in actor.permissions or actor.kind is not ActorKind.human:
            await self._audit_denied(actor, execution_id, "reconciliation", "permission", target_type="execution")
            raise ApprovalDeniedError()
        if not result_ref.strip():
            raise ValueError("result_ref must not be blank")
        execution = await self._repo.get_approval_execution(execution_id, guild_id=str(actor.guild_id))
        if execution is None:
            await self._audit_denied(actor, execution_id, "reconciliation", "not_found", target_type="execution")
            raise ApprovalNotFoundError()
        approval = await self._repo.get_approval_request(int(execution["approval_id"]), guild_id=str(actor.guild_id))
        if approval is None:
            await self._audit_denied(actor, execution_id, "reconciliation", "not_found", target_type="execution")
            raise ApprovalNotFoundError()
        if execution["state"] == ExecutionState.executed.value:
            return self._execution_from_row(execution)
        if execution["state"] != ExecutionState.executing.value or approval["state"] != ApprovalState.executing.value:
            raise ApprovalConflictError()
        applied = await self._repo.reconcile_approval_execution(
            execution_id,
            approval_id=int(approval["id"]),
            guild_id=str(actor.guild_id),
            actor_id=actor.actor_id,
            finished_at=_iso(_as_utc(self._clock())),
            result_ref=result_ref,
        )
        if not applied:
            raise ApprovalConflictError()
        current = await self._repo.get_approval_execution(execution_id, guild_id=str(actor.guild_id))
        if current is None:  # pragma: no cover - storage invariant
            raise ApprovalConflictError()
        return self._execution_from_row(current)

    async def get_execution(self, execution_id: int, *, actor: ApprovalActor) -> ExecutionAttempt:
        execution = await self._repo.get_approval_execution(execution_id, guild_id=str(actor.guild_id))
        if execution is None:
            raise ApprovalNotFoundError()
        approval = await self._repo.get_approval_request(int(execution["approval_id"]), guild_id=str(actor.guild_id))
        if approval is None:
            raise ApprovalNotFoundError()
        return self._execution_from_row(execution)

    # ---------------------------------------------------------------- internals

    def _require_binding(self, row: Any, request: ExecutionRequest) -> None:
        binding = request.binding
        if (
            row["action_type"] != binding.action_type
            or row["target_type"] != binding.target_type
            or row["target_id"] != binding.target_id
            or row["content_version"] != binding.content_version
            or row["content_hash"] != _content_hash(binding.content)
        ):
            raise ApprovalBindingError()

    async def _validate_binding(self, row: Any, request: ExecutionRequest, actor: ApprovalActor) -> None:
        try:
            self._require_binding(row, request)
        except ApprovalBindingError:
            await self._audit_denied(actor, request.approval_id, "execution", "binding_mismatch")
            raise

    async def _raise_after_failed_transition(self, approval_id: int, actor: ApprovalActor, now_iso: str) -> None:
        current = await self._repo.get_approval_request(approval_id, guild_id=str(actor.guild_id))
        if current is None:
            raise ApprovalNotFoundError()
        if self._is_expired(current, now_iso):
            await self._expire_row(current, now_iso)
            raise ApprovalExpiredError()
        if current["state"] == ApprovalState.expired.value:
            raise ApprovalExpiredError()
        raise ApprovalConflictError()

    async def _audit_denied(
        self,
        actor: ApprovalActor,
        target_id: int,
        action: str,
        reason: str,
        *,
        target_type: str = "approval",
    ) -> None:
        await self._repo.audit(
            actor_id=actor.actor_id,
            action=f"approval.{action}_denied",
            target_type=target_type,
            target_id=str(target_id),
            guild_id=str(actor.guild_id),
            meta={"reason": reason, "guild_id": actor.guild_id},
        )

    @staticmethod
    def _is_expired(row: Any, now_iso: str) -> bool:
        return (
            row["state"] in (ApprovalState.pending.value, ApprovalState.approved.value)
            and str(row["expires_at"]) <= now_iso
        )

    @staticmethod
    def _request_from_row(row: Any) -> ApprovalRequest:
        return ApprovalRequest.model_validate(dict(row))

    @staticmethod
    def _execution_from_row(row: Any) -> ExecutionAttempt:
        return ExecutionAttempt.model_validate(dict(row))
