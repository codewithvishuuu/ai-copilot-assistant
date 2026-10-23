"""Approved Discord publishing boundary.

The only path to a public post is ``ApprovalService.begin_execution_claim`` and
an immediate adapter send. A replay or an uncertain send never sends again:
the claim is left ``executing`` and can only be reconciled by finding the
Discord message that carries the deterministic nonce. Recovery never resets a
claim without that evidence.
"""

from __future__ import annotations

import hashlib
import logging
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ..db.repository import Repository
from ..errors import ApprovalDeniedError, ContentPublishingDisabledError, ContentTargetError
from .approvals import (
    ActionBinding,
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
    ApprovalService,
    ExecutionOutcome,
    ExecutionRequest,
)
from .content_studio import (
    ContentStudio,
    ContentTarget,
    DraftStatus,
    PublishCandidate,
)

log = logging.getLogger(__name__)


class SendRejected(Exception):
    """The adapter knows Discord did not accept the message."""

    def __init__(self, error_code: str, detail: str | None = None) -> None:
        super().__init__(detail or error_code)
        self.error_code = error_code
        self.detail = detail


class SendAmbiguous(Exception):
    """The adapter cannot tell whether Discord accepted the message."""

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "ambiguous send")
        self.detail = detail


class ContentSender(Protocol):
    async def verify_actor(self, actor: ApprovalActor) -> bool: ...

    async def resolve_target(self, guild_id: int, channel_id: int) -> ContentTarget: ...

    async def send_content(self, *, guild_id: int, channel_id: int, content: str, nonce: str) -> str: ...

    async def find_content_message(self, *, guild_id: int, channel_id: int, content: str, nonce: str) -> str | None: ...


class PublishStatus(str, Enum):
    published = "published"
    failed = "failed"
    ambiguous = "ambiguous"


class PublishResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PublishStatus
    execution_id: int | None = None
    message_id: str | None = None
    recorded: bool = True
    error_code: str | None = None
    detail: str | None = None


def publication_key(candidate: PublishCandidate) -> str:
    return f"content:{candidate.draft_id}:v{candidate.content_version}:a{candidate.approval_id}"


def publication_nonce(candidate: PublishCandidate) -> str:
    """Bounded, deterministic nonce used only to reconcile an ambiguous send."""
    digest = hashlib.sha256(publication_key(candidate).encode("utf-8")).hexdigest()[:20]
    return f"cc{digest}"


class ContentPublisher:
    def __init__(
        self,
        studio: ContentStudio,
        approvals: ApprovalService,
        repo: Repository,
        *,
        settings,
    ) -> None:
        self._studio = studio
        self._approvals = approvals
        self._repo = repo
        self._settings = settings

    async def publish(self, actor: ApprovalActor, draft_id: int, *, sender: ContentSender) -> PublishResult:
        if not self._settings.content_publishing_enabled:
            raise ContentPublishingDisabledError()
        if ApprovalPermission.execute not in actor.permissions:
            # ApprovalService rechecks too; this avoids even resolving a target.
            raise ApprovalDeniedError()
        if actor.kind is not ActorKind.human:
            raise ApprovalDeniedError()
        _require_allowed_guild(self._settings, actor.guild_id)

        current = await self._studio.get_draft(actor, draft_id)
        if current.status is DraftStatus.published:
            if current.published_message_id:
                return PublishResult(
                    status=PublishStatus.published,
                    execution_id=current.execution_id,
                    message_id=current.published_message_id,
                    recorded=True,
                )
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=current.execution_id,
                recorded=False,
                detail="This draft is marked published but has no recorded message id.",
            )
        if current.status is DraftStatus.executing:
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=current.execution_id,
                recorded=False,
                detail=(
                    "This draft already has an execution in progress. It was not sent again; "
                    "use content recovery to reconcile it."
                ),
            )
        _require_allowed_channel(self._settings, current.target_channel_id)

        candidate = await self._studio.get_publish_candidate(actor, draft_id)
        await _revalidate_actor(sender, actor)
        target = await sender.resolve_target(candidate.guild_id, candidate.target_channel_id)
        _require_target(candidate, target)

        key = publication_key(candidate)
        existing = await self._repo.get_approval_execution_by_key(key)
        if existing is not None:
            return await self._existing_result(existing, candidate, actor)

        request = ExecutionRequest(
            approval_id=candidate.approval_id,
            binding=ActionBinding(
                action_type="content.publish",
                guild_id=candidate.guild_id,
                target_type="channel",
                target_id=str(candidate.target_channel_id),
                content_version=candidate.content_version,
                content=candidate.content,
            ),
            idempotency_key=key,
        )
        attempt, created = await self._approvals.begin_execution_claim(request, actor=actor)
        if not created:
            row = await self._repo.get_approval_execution(attempt.id)
            if row is None:  # pragma: no cover - storage invariant
                return PublishResult(
                    status=PublishStatus.ambiguous,
                    execution_id=attempt.id,
                    recorded=False,
                    detail="Execution claim exists but its row could not be read.",
                )
            return await self._existing_result(row, candidate, actor)

        nonce = publication_nonce(candidate)
        try:
            message_id = await sender.send_content(
                guild_id=candidate.guild_id,
                channel_id=candidate.target_channel_id,
                content=candidate.content,
                nonce=nonce,
            )
        except SendRejected as exc:
            return await self._record_failure(attempt.id, candidate, actor, exc.error_code)
        except SendAmbiguous as exc:
            await self._audit_ambiguous(candidate, attempt.id)
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=attempt.id,
                recorded=False,
                detail=exc.detail or "Discord did not confirm whether the message was sent.",
            )
        except Exception as exc:  # noqa: BLE001 - an unknown post-send outcome is unsafe to retry
            log.warning("Content send outcome is ambiguous (%s)", type(exc).__name__)
            await self._audit_ambiguous(candidate, attempt.id)
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=attempt.id,
                recorded=False,
                detail="The send raised an unknown error; the claim was left for reconciliation.",
            )

        if not message_id:
            await self._audit_ambiguous(candidate, attempt.id)
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=attempt.id,
                recorded=False,
                detail="Discord returned no message id; the claim was left for reconciliation.",
            )
        return await self._record_success(attempt.id, candidate, actor, message_id)

    async def recover(self, actor: ApprovalActor, draft_id: int, *, sender: ContentSender) -> PublishResult:
        """Reconcile a stuck claim only when the message can be proven to exist."""
        _require_allowed_guild(self._settings, actor.guild_id)
        candidate = await self._studio.get_recovery_candidate(actor, draft_id)
        _require_allowed_channel(self._settings, candidate.target_channel_id)
        await _revalidate_actor(sender, actor)
        nonce = publication_nonce(candidate)
        try:
            message_id = await sender.find_content_message(
                guild_id=candidate.guild_id,
                channel_id=candidate.target_channel_id,
                content=candidate.content,
                nonce=nonce,
            )
        except Exception as exc:  # noqa: BLE001 - inability to check is not evidence
            log.warning("Content reconciliation lookup failed (%s)", type(exc).__name__)
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=candidate.execution_id,
                recorded=False,
                detail="Could not check the channel, so the execution was left unchanged.",
            )
        if not message_id:
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=candidate.execution_id,
                recorded=False,
                detail=(
                    "No message with this draft's nonce was found. The execution was left "
                    "executing; it was not reset or re-sent."
                ),
            )
        try:
            await self._approvals.reconcile_execution(
                candidate.execution_id,
                actor=actor,
                result_ref=message_id,
            )
        except Exception:  # noqa: BLE001 - leave the claim for another review
            log.exception("Could not record reconciled content execution")
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=candidate.execution_id,
                recorded=False,
                detail="The message was found but its execution could not be recorded.",
            )
        await self._mark_published(candidate, message_id)
        return PublishResult(
            status=PublishStatus.published,
            execution_id=candidate.execution_id,
            message_id=message_id,
            recorded=True,
        )

    # ---------------------------------------------------------------- internals

    async def _existing_result(self, row, candidate: PublishCandidate, actor: ApprovalActor) -> PublishResult:
        if int(row["approval_id"]) != candidate.approval_id or str(row["executor_id"]) != actor.actor_id:
            return PublishResult(
                status=PublishStatus.failed,
                execution_id=int(row["id"]),
                recorded=False,
                error_code="execution_owner_mismatch",
                detail="This draft already has an execution owned by a different actor.",
            )
        state = str(row["state"])
        if state == "executed":
            message_id = row["result_ref"]
            if message_id:
                await self._mark_published(candidate, str(message_id))
                return PublishResult(
                    status=PublishStatus.published,
                    execution_id=int(row["id"]),
                    message_id=str(message_id),
                    recorded=True,
                )
            return PublishResult(
                status=PublishStatus.ambiguous,
                execution_id=int(row["id"]),
                recorded=False,
                detail="The execution is marked executed but has no recorded message id.",
            )
        if state == "failed":
            return PublishResult(
                status=PublishStatus.failed,
                execution_id=int(row["id"]),
                recorded=True,
                error_code=str(row["error_code"] or "publish_failed"),
                detail="This publication attempt already failed.",
            )
        return PublishResult(
            status=PublishStatus.ambiguous,
            execution_id=int(row["id"]),
            recorded=False,
            detail=(
                "This publication attempt is already executing. It was not sent again; "
                "use content recovery to reconcile it."
            ),
        )

    async def _record_success(
        self,
        execution_id: int,
        candidate: PublishCandidate,
        actor: ApprovalActor,
        message_id: str,
    ) -> PublishResult:
        recorded = True
        try:
            await self._approvals.finish_execution(
                execution_id,
                actor=actor,
                outcome=ExecutionOutcome.executed,
                result_ref=message_id,
            )
        except Exception:  # noqa: BLE001 - the side effect happened; never retry it
            recorded = await self._execution_is_recorded(execution_id, message_id)
            if not recorded:
                log.exception("Discord message sent but its execution could not be recorded")
                await self._audit_ambiguous(candidate, execution_id)
                return PublishResult(
                    status=PublishStatus.published,
                    execution_id=execution_id,
                    message_id=message_id,
                    recorded=False,
                    detail=(
                        "Discord confirmed the message, but the local execution record could "
                        "not be completed. The claim was left for recovery; do not resend."
                    ),
                )
        await self._mark_published(candidate, message_id)
        return PublishResult(
            status=PublishStatus.published,
            execution_id=execution_id,
            message_id=message_id,
            recorded=True,
        )

    async def _record_failure(
        self,
        execution_id: int,
        candidate: PublishCandidate,
        actor: ApprovalActor,
        error_code: str,
    ) -> PublishResult:
        recorded = True
        try:
            await self._approvals.finish_execution(
                execution_id,
                actor=actor,
                outcome=ExecutionOutcome.failed,
                error_code=error_code,
            )
        except Exception:  # noqa: BLE001 - report an unrecorded failure truthfully
            recorded = False
            log.exception("Could not record definite content publication failure")
        if recorded:
            try:
                await self._studio.mark_failed(
                    candidate.draft_id,
                    guild_id=candidate.guild_id,
                    approval_id=candidate.approval_id,
                    error_code=error_code,
                )
            except Exception:  # noqa: BLE001 - the approval itself is already failed
                log.exception("Could not update content draft failure state")
        return PublishResult(
            status=PublishStatus.failed,
            execution_id=execution_id,
            recorded=recorded,
            error_code=error_code,
            detail="Discord rejected the message; nothing was published.",
        )

    async def _execution_is_recorded(self, execution_id: int, message_id: str) -> bool:
        try:
            row = await self._repo.get_approval_execution(execution_id)
        except Exception:  # noqa: BLE001 - unknown is not success
            return False
        return bool(row is not None and str(row["state"]) == "executed" and str(row["result_ref"] or "") == message_id)

    async def _mark_published(self, candidate: PublishCandidate, message_id: str) -> None:
        try:
            await self._studio.mark_published(
                candidate.draft_id,
                guild_id=candidate.guild_id,
                approval_id=candidate.approval_id,
                message_id=message_id,
            )
        except Exception:  # noqa: BLE001 - approval execution already proves success
            log.exception("Could not update content draft published state")

    async def _audit_ambiguous(self, candidate: PublishCandidate, execution_id: int) -> None:
        try:
            await self._studio.audit_ambiguous(candidate, execution_id=execution_id)
        except Exception:  # noqa: BLE001 - audit failure must not trigger a retry
            log.exception("Could not audit ambiguous content publication")


def _require_target(candidate: PublishCandidate, target: ContentTarget) -> None:
    if (
        target.guild_id != candidate.guild_id
        or target.channel_id != candidate.target_channel_id
        or not target.exists
        or not target.can_send
    ):
        raise ContentTargetError()


def _require_allowed_guild(settings, guild_id: int) -> None:
    if guild_id not in set(settings.guild_allowlist):
        raise ApprovalDeniedError()


def _require_allowed_channel(settings, channel_id: int) -> None:
    if settings.channel_allowlist and channel_id not in set(settings.channel_allowlist):
        raise ContentTargetError("The target channel is outside the configured allowlist.")


async def _revalidate_actor(sender: ContentSender, actor: ApprovalActor) -> None:
    try:
        allowed = await sender.verify_actor(actor)
    except Exception:  # noqa: BLE001 - inability to recheck authority fails closed
        log.warning("Could not revalidate content actor; denying publication")
        raise ApprovalDeniedError() from None
    if not allowed:
        raise ApprovalDeniedError()
