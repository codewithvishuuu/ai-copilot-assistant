"""Content Studio: typed drafts, immutable versions, generation and review.

Drafts are application data, never model authority. Generation only produces
text; a separate human approval through ``ApprovalService`` is required before
the publishing module is allowed to claim an execution. Every draft operation is
scoped to the trusted actor's guild, and every edit invalidates an older
approval rather than reusing it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db.repository import Repository
from ..errors import (
    ApprovalBindingError,
    ApprovalDeniedError,
    ApprovalExpiredError,
    ApprovalSelfApprovalError,
    ContentDraftConflictError,
    ContentDraftNotFoundError,
    ContentTargetError,
    ContentValidationError,
    OutputValidationError,
)
from ..llm import prompts
from ..llm.base import LLMProvider
from ..llm.schemas import ContentDraftOutput, content_draft_json_schema, parse_content_draft
from .approvals import (
    ActionRequest,
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
    ApprovalService,
    ApprovalState,
)
from .content_voice import (
    VoiceProfile,
    load_voice_profile,
    recent_feedback_prompt,
    similarity_ratio,
)

log = logging.getLogger(__name__)

MAX_TOPIC_CHARS = 500
MAX_AUDIENCE_CHARS = 200
MAX_TONE_CHARS = 100
MAX_PURPOSE_CHARS = 300
MAX_FINDINGS = 20
_CONCURRENT_EDIT_MESSAGE = "This draft changed while you were working; reopen it and try again."


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return _as_utc(moment).isoformat(timespec="seconds")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ContentType(str, Enum):
    announcement = "announcement"
    educational_post = "educational_post"
    poll_proposal = "poll_proposal"
    event_announcement = "event_announcement"


class ContentLength(str, Enum):
    short = "short"
    medium = "medium"
    long = "long"

    def max_chars(self, cap: int) -> int:
        return {"short": 500, "medium": 1000, "long": cap}[self.value]


class DraftStatus(str, Enum):
    draft = "draft"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    cancelled = "cancelled"
    executing = "executing"
    published = "published"
    failed = "failed"


class ValidationSeverity(str, Enum):
    error = "error"
    warning = "warning"


class ValidationFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=500)
    severity: ValidationSeverity = ValidationSeverity.error


class ContentTarget(BaseModel):
    """Trusted target facts resolved by the Discord adapter immediately before use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    guild_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    exists: bool
    can_send: bool


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content_type: ContentType
    topic: str = Field(min_length=3, max_length=MAX_TOPIC_CHARS)
    audience: str = Field(default="", max_length=MAX_AUDIENCE_CHARS)
    tone: str = Field(default="", max_length=MAX_TONE_CHARS)
    purpose: str = Field(default="", max_length=MAX_PURPOSE_CHARS)
    length: ContentLength = ContentLength.medium
    formatting: str = Field(default="", max_length=200)
    emoji_policy: Literal["", "none", "sparing", "allowed"] = ""


class ContentDraftView(BaseModel):
    """One exact draft version plus derived review state and actor capabilities."""

    model_config = ConfigDict(extra="forbid")

    draft_id: int
    guild_id: int
    target_channel_id: int
    content_type: ContentType
    topic: str
    audience: str
    tone: str
    purpose: str
    author_id: str
    created_at: str
    updated_at: str
    version: int
    content: str
    content_hash: str
    source_kind: str = "manual"
    source_ref: str | None = None
    findings: list[ValidationFinding] = Field(default_factory=list)
    status: DraftStatus
    approval_id: int | None = None
    approval_state: str | None = None
    approval_expires_at: str | None = None
    execution_id: int | None = None
    execution_state: str | None = None
    execution_started_at: str | None = None
    published_message_id: str | None = None
    can_edit: bool = False
    can_submit: bool = False
    can_approve: bool = False
    can_reject: bool = False
    can_cancel: bool = False
    can_publish: bool = False
    can_recover: bool = False

    def validation_errors(self) -> list[ValidationFinding]:
        return [finding for finding in self.findings if finding.severity is ValidationSeverity.error]

    def validation_warnings(self) -> list[ValidationFinding]:
        return [finding for finding in self.findings if finding.severity is ValidationSeverity.warning]


class PublishCandidate(BaseModel):
    """The exact immutable version an executor may attempt to publish."""

    model_config = ConfigDict(extra="forbid")

    draft_id: int
    guild_id: int
    target_channel_id: int
    approval_id: int
    content_version: str
    content: str
    content_hash: str
    status: DraftStatus


class RecoveryCandidate(PublishCandidate):
    """An uncertain execution claim that may be reconciled, never re-sent."""

    execution_id: int
    execution_state: str
    execution_started_at: str


class ContentGenerator:
    """One bounded provider call (plus one repair) producing validated draft text."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_chars: int,
        timeout_s: float,
        max_tokens: int,
        max_context_chars: int,
    ) -> None:
        self._provider = provider
        self._max_chars = max_chars
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._max_context_chars = max_context_chars

    async def generate(
        self,
        request: DraftRequest,
        *,
        context: Sequence[tuple[str, str]] = (),
        voice: VoiceProfile | None = None,
        feedback: Sequence[str] = (),
        approved_examples: Sequence[str] = (),
    ) -> ContentDraftOutput:
        max_chars = request.length.max_chars(self._max_chars)
        bounded_context = _bounded_context(context, self._max_context_chars)
        voice_lines = voice.prompt_lines() if voice is not None else []
        user = prompts.build_content_prompt(
            content_type=request.content_type.value,
            topic=request.topic,
            audience=request.audience,
            tone=request.tone,
            purpose=request.purpose,
            max_chars=max_chars,
            context=bounded_context,
            voice_lines=voice_lines,
            feedback=feedback,
            approved_examples=_bounded_examples(approved_examples, self._max_context_chars),
            formatting=request.formatting or (voice.formatting if voice is not None else ""),
            emoji_policy=request.emoji_policy or (voice.emoji_policy if voice is not None else ""),
        )
        _, output, _repair = await self._chat_validated(
            system=prompts.SYSTEM_CONTENT,
            user=user,
            schema=content_draft_json_schema(),
            parser=parse_content_draft,
        )
        if not output.has_content():
            raise OutputValidationError("The model returned an empty content draft.")
        if len(output.content) > max_chars:
            raise OutputValidationError(f"The model draft exceeded the requested {max_chars}-character limit.")
        return output

    async def _chat_validated(self, *, system: str, user: str, schema: dict, parser):
        try:
            raw = await self._provider.chat(
                system=system,
                user=user,
                json_schema=schema,
                timeout_s=self._timeout_s,
                max_tokens=self._max_tokens,
            )
            return raw, parser(raw), False
        except OutputValidationError as first_error:
            log.info(
                "Content model output failed validation (%s); attempting one repair",
                type(first_error).__name__,
            )
            repair = prompts.build_repair_prompt(user, str(first_error))
            raw = await self._provider.chat(
                system=system,
                user=repair,
                json_schema=schema,
                timeout_s=self._timeout_s,
                max_tokens=self._max_tokens,
            )
            return raw, parser(raw), True


class ContentStudio:
    """Guild-scoped draft lifecycle built on the Phase 2A approval engine."""

    def __init__(
        self,
        repo: Repository,
        approvals: ApprovalService,
        *,
        settings: Any,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._approvals = approvals
        self._settings = settings
        self._clock = clock

    # ---------------------------------------------------------------- generation

    async def generate_draft(
        self,
        actor: ApprovalActor,
        request: DraftRequest,
        *,
        target: ContentTarget,
        provider: LLMProvider,
        source_kind: str = "manual",
        source_ref: str | None = None,
    ) -> ContentDraftView:
        self._require(actor, ApprovalPermission.propose)
        self._validate_target(actor, target)
        if source_ref is not None:
            existing = await self._repo.get_content_draft_by_source(
                guild_id=str(actor.guild_id),
                source_kind=source_kind,
                source_ref=source_ref,
            )
            if existing is not None:
                # Scheduled retry after a crash: the draft already exists, so
                # return it instead of generating (and storing) a duplicate.
                return await self.get_draft(actor, int(existing["id"]))
        context = await self._context(request.topic, guild_id=actor.guild_id)
        voice = await self._voice(actor.guild_id)
        feedback = await self._feedback(actor.guild_id)
        examples = await self._approved_examples(actor.guild_id)
        generator = ContentGenerator(
            provider,
            max_chars=self._settings.content_generation_max_chars,
            timeout_s=self._settings.content_generation_timeout_s,
            max_tokens=self._settings.content_generation_max_tokens,
            max_context_chars=self._settings.content_context_max_chars,
        )
        output = await generator.generate(
            request,
            context=context,
            voice=voice,
            feedback=feedback,
            approved_examples=examples,
        )
        extra: list[ValidationFinding] = []
        if output.needs_research:
            detail = output.research_notes or "Current facts needed for this topic were not available."
            extra.append(
                ValidationFinding(
                    code="needs_research",
                    message=f"Research required before approval: {detail}"[:500],
                )
            )
        for warning in output.warnings:
            extra.append(
                ValidationFinding(
                    code="model_warning",
                    message=warning,
                    severity=ValidationSeverity.warning,
                )
            )
        return await self.create_draft(
            actor,
            request,
            output.content,
            target=target,
            extra=extra,
            allow_blocking=True,
            source_kind=source_kind,
            source_ref=source_ref,
        )

    async def create_draft(
        self,
        actor: ApprovalActor,
        request: DraftRequest,
        content: str,
        *,
        target: ContentTarget,
        extra: Sequence[ValidationFinding] = (),
        allow_blocking: bool = False,
        source_kind: str = "manual",
        source_ref: str | None = None,
    ) -> ContentDraftView:
        """Store a new draft and immutable version 1 from trusted application input.

        ``allow_blocking`` is used by generation so a factual-research warning is
        preserved as a reviewable draft instead of losing the model output. Such a
        version still cannot be submitted until a human edits it.
        """
        self._require(actor, ApprovalPermission.propose)
        content = content.strip()
        self._validate_target(actor, target)
        findings = self._validate_content(content, guild_id=actor.guild_id, extra=extra)
        if not any(item.severity is ValidationSeverity.error for item in findings):
            duplicate = await self._duplicate_check(content, guild_id=actor.guild_id, exclude_draft_id=None)
            if duplicate is not None:
                findings.append(duplicate)
            else:
                near = await self._near_duplicate_check(
                    content, guild_id=actor.guild_id, exclude_draft_id=None
                )
                if near is not None:
                    findings.append(near)
        blocking = any(item.severity is ValidationSeverity.error for item in findings)
        if blocking and (not allow_blocking or not content.strip()):
            raise ContentValidationError(detail=_findings_detail(findings))

        now = _iso(self._clock())
        draft_id = await self._repo.insert_content_draft_with_version(
            guild_id=str(actor.guild_id),
            target_channel_id=str(target.channel_id),
            content_type=request.content_type.value,
            topic=request.topic,
            audience=request.audience,
            tone=request.tone,
            purpose=request.purpose,
            created_by=actor.actor_id,
            created_at=now,
            content=content,
            content_hash=content_hash(content),
            validation_json=_dump_findings(findings),
            source_kind=source_kind,
            source_ref=source_ref,
        )
        return await self.get_draft(actor, draft_id)

    # -------------------------------------------------------------------- edits

    async def edit_draft(
        self,
        actor: ApprovalActor,
        draft_id: int,
        *,
        expected_version: int,
        content: str | None = None,
        target: ContentTarget | None = None,
    ) -> ContentDraftView:
        """Create a new immutable version, cancelling any live old approval first."""
        self._require(actor, ApprovalPermission.propose)
        draft, version, approval, execution = await self._bundle(actor, draft_id)
        if int(draft["current_version"]) != expected_version:
            raise ContentDraftConflictError(_CONCURRENT_EDIT_MESSAGE)
        status = self._status(draft, approval, execution)
        if status in (DraftStatus.executing, DraftStatus.published):
            raise ContentDraftConflictError(
                "A draft that is executing or published cannot be edited. Create a new draft."
            )
        new_content = str(version["content"]) if content is None else content.strip()
        new_channel = int(draft["target_channel_id"]) if target is None else target.channel_id
        if target is not None:
            self._validate_target(actor, target)
        if new_content == version["content"] and new_channel == int(draft["target_channel_id"]):
            return await self.get_draft(actor, draft_id)

        # Validate before touching the old approval: a rejected edit must leave
        # the previously approved version intact and publishable.
        findings = self._validate_content(new_content, guild_id=actor.guild_id, exclude_draft_id=draft_id)
        if not any(item.severity is ValidationSeverity.error for item in findings):
            duplicate = await self._duplicate_check(new_content, guild_id=actor.guild_id, exclude_draft_id=draft_id)
            if duplicate is not None:
                findings.append(duplicate)
            else:
                near = await self._near_duplicate_check(
                    new_content, guild_id=actor.guild_id, exclude_draft_id=draft_id
                )
                if near is not None:
                    findings.append(near)
        if any(item.severity is ValidationSeverity.error for item in findings):
            raise ContentValidationError(detail=_findings_detail(findings))

        if approval is not None:
            await self._invalidate_approval(actor, draft, approval)
            cleared = await self._repo.clear_content_draft_approval(
                draft_id,
                guild_id=str(actor.guild_id),
                expected_version=int(draft["current_version"]),
                approval_id=int(approval["id"]),
                updated_at=_iso(self._clock()),
            )
            if not cleared:
                raise ContentDraftConflictError(_CONCURRENT_EDIT_MESSAGE)

        applied = await self._repo.add_content_draft_version(
            draft_id,
            guild_id=str(actor.guild_id),
            expected_version=int(draft["current_version"]),
            target_channel_id=str(new_channel),
            created_by=actor.actor_id,
            created_at=_iso(self._clock()),
            content=new_content,
            content_hash=content_hash(new_content),
            validation_json=_dump_findings(findings),
        )
        if not applied:
            raise ContentDraftConflictError(_CONCURRENT_EDIT_MESSAGE)
        return await self.get_draft(actor, draft_id)

    # ------------------------------------------------------------------- submit

    async def submit_draft(self, actor: ApprovalActor, draft_id: int, *, target: ContentTarget) -> ContentDraftView:
        """Request human approval for the exact current version and target."""
        self._require(actor, ApprovalPermission.propose)
        draft, version, approval, execution = await self._bundle(actor, draft_id)
        status = self._status(draft, approval, execution)
        if status is not DraftStatus.draft or approval is not None:
            raise ContentDraftConflictError(
                "This draft already has a review history. Create a new version before resubmitting."
            )
        self._validate_target(actor, target, expected_channel_id=int(draft["target_channel_id"]))
        findings = load_findings(version["validation_json"])
        if any(item.severity is ValidationSeverity.error for item in findings):
            raise ContentValidationError(detail=_findings_detail(findings))

        requester = ApprovalActor(
            actor_id=str(version["created_by"]),
            guild_id=actor.guild_id,
            kind=ActorKind.human,
            permissions=frozenset({ApprovalPermission.propose}),
        )
        request = ActionRequest(
            action_type="content.publish",
            guild_id=actor.guild_id,
            target_type="channel",
            target_id=str(draft["target_channel_id"]),
            content_version=str(draft["current_version"]),
            content=str(version["content"]),
            expires_in_s=self._settings.content_approval_ttl_s,
        )
        approval_request = await self._approvals.request_action(requester, request)
        attached = await self._repo.set_content_draft_approval(
            draft_id,
            guild_id=str(actor.guild_id),
            expected_version=int(draft["current_version"]),
            approval_id=approval_request.id,
            submitted_by=actor.actor_id,
            updated_at=_iso(self._clock()),
        )
        if not attached:
            await self._cancel_orphan_approval(actor.guild_id, approval_request.id)
            raise ContentDraftConflictError(_CONCURRENT_EDIT_MESSAGE)
        return await self.get_draft(actor, draft_id)

    # ----------------------------------------------------------------- decisions

    async def approve_draft(self, actor: ApprovalActor, draft_id: int, *, note: str | None = None) -> ContentDraftView:
        draft, version, approval, execution = await self._bundle(actor, draft_id)
        if approval is None:
            raise ContentDraftConflictError("This draft has no pending approval request.")
        if str(version["created_by"]) == actor.actor_id:
            raise ApprovalSelfApprovalError()
        self._require_current_binding(draft, approval, version)
        await self._approvals.approve(int(approval["id"]), actor=actor, note=note)
        return await self.get_draft(actor, draft_id)

    async def reject_draft(self, actor: ApprovalActor, draft_id: int, *, note: str | None = None) -> ContentDraftView:
        draft, version, approval, _execution = await self._bundle(actor, draft_id)
        if approval is None:
            raise ContentDraftConflictError("This draft has no pending approval request.")
        self._require_current_binding(draft, approval, version)
        await self._approvals.reject(int(approval["id"]), actor=actor, note=note)
        return await self.get_draft(actor, draft_id)

    async def cancel_draft(self, actor: ApprovalActor, draft_id: int, *, reason: str | None = None) -> ContentDraftView:
        draft, _version, approval, _execution = await self._bundle(actor, draft_id)
        if approval is None:
            raise ContentDraftConflictError("This draft has no live approval request to cancel.")
        await self._approvals.cancel(int(approval["id"]), actor=actor, reason=reason)
        return await self.get_draft(actor, draft_id)

    # -------------------------------------------------------------------- reads

    async def get_draft(self, actor: ApprovalActor, draft_id: int) -> ContentDraftView:
        self._require_read(actor)
        await self._approvals.expire_due()
        draft, version, approval, execution = await self._bundle(actor, draft_id)
        return self._view(actor, draft, version, approval, execution)

    async def list_drafts(
        self,
        actor: ApprovalActor,
        *,
        status: DraftStatus | None = None,
        limit: int = 25,
    ) -> list[ContentDraftView]:
        self._require_read(actor)
        await self._approvals.expire_due()
        rows = await self._repo.list_content_drafts(
            guild_id=str(actor.guild_id),
            status=status.value if status is not None else None,
            limit=limit,
        )
        views: list[ContentDraftView] = []
        for row in rows:
            draft, version, approval, execution = await self._bundle(actor, int(row["id"]))
            views.append(self._view(actor, draft, version, approval, execution))
        return views

    async def get_publish_candidate(self, actor: ApprovalActor, draft_id: int) -> PublishCandidate:
        """Return the exact approved version an executor may attempt once more."""
        self._require(actor, ApprovalPermission.execute)
        if actor.kind is not ActorKind.human:
            raise ApprovalDeniedError()
        await self._approvals.expire_due()
        draft, version, approval, execution = await self._bundle(actor, draft_id)
        status = self._status(draft, approval, execution)
        if status is not DraftStatus.approved or approval is None:
            raise ContentDraftConflictError("This draft does not have a live human approval.")
        self._require_current_binding(draft, approval, version)
        return PublishCandidate(
            draft_id=draft_id,
            guild_id=actor.guild_id,
            target_channel_id=int(draft["target_channel_id"]),
            approval_id=int(approval["id"]),
            content_version=str(draft["current_version"]),
            content=str(version["content"]),
            content_hash=str(version["content_hash"]),
            status=status,
        )

    async def get_recovery_candidate(self, actor: ApprovalActor, draft_id: int) -> RecoveryCandidate:
        """Return an uncertain claim only; recovery may record or leave it, never send."""
        self._require(actor, ApprovalPermission.execute)
        if actor.kind is not ActorKind.human:
            raise ApprovalDeniedError()
        draft, version, approval, execution = await self._bundle(actor, draft_id)
        status = self._status(draft, approval, execution)
        if status is not DraftStatus.executing or approval is None or execution is None:
            raise ContentDraftConflictError("This draft has no uncertain execution to reconcile.")
        self._require_current_binding(draft, approval, version)
        return RecoveryCandidate(
            draft_id=draft_id,
            guild_id=actor.guild_id,
            target_channel_id=int(draft["target_channel_id"]),
            approval_id=int(approval["id"]),
            content_version=str(draft["current_version"]),
            content=str(version["content"]),
            content_hash=str(version["content_hash"]),
            status=status,
            execution_id=int(execution["id"]),
            execution_state=str(execution["state"]),
            execution_started_at=str(execution["started_at"]),
        )

    async def mark_published(
        self,
        draft_id: int,
        *,
        guild_id: int,
        approval_id: int,
        message_id: str,
    ) -> None:
        changed = await self._repo.set_content_draft_state(
            draft_id,
            guild_id=str(guild_id),
            state="published",
            updated_at=_iso(self._clock()),
            expected_approval_id=approval_id,
        )
        await self._repo.audit(
            actor_id="system",
            action="content.published" if changed else "content.published_already_recorded",
            target_type="content_draft",
            target_id=str(draft_id),
            guild_id=str(guild_id),
            meta={"guild_id": str(guild_id), "approval_id": approval_id, "message_id": message_id},
        )

    async def mark_failed(
        self,
        draft_id: int,
        *,
        guild_id: int,
        approval_id: int,
        error_code: str,
    ) -> None:
        changed = await self._repo.set_content_draft_state(
            draft_id,
            guild_id=str(guild_id),
            state="failed",
            updated_at=_iso(self._clock()),
            expected_approval_id=approval_id,
        )
        await self._repo.audit(
            actor_id="system",
            action="content.publish_failed" if changed else "content.publish_failure_recorded",
            target_type="content_draft",
            target_id=str(draft_id),
            guild_id=str(guild_id),
            meta={"guild_id": str(guild_id), "approval_id": approval_id, "error_code": error_code},
        )

    async def audit_ambiguous(self, candidate: PublishCandidate, *, execution_id: int) -> None:
        await self._repo.audit(
            actor_id="system",
            action="content.publish_ambiguous",
            target_type="content_draft",
            target_id=str(candidate.draft_id),
            guild_id=str(candidate.guild_id),
            meta={
                "guild_id": str(candidate.guild_id),
                "approval_id": candidate.approval_id,
                "execution_id": execution_id,
                "version": candidate.content_version,
            },
        )

    # ---------------------------------------------------------------- internals

    async def _bundle(self, actor: ApprovalActor, draft_id: int):
        draft = await self._repo.get_content_draft(draft_id, guild_id=str(actor.guild_id))
        if draft is None:
            raise ContentDraftNotFoundError()
        version = await self._repo.get_content_draft_version(
            draft_id, int(draft["current_version"]), guild_id=str(actor.guild_id)
        )
        if version is None:  # pragma: no cover - storage invariant
            raise ContentDraftConflictError("The draft's current version is missing.")
        approval = None
        execution = None
        if draft["approval_id"] is not None:
            approval = await self._repo.get_approval_request(int(draft["approval_id"]), guild_id=str(actor.guild_id))
            if approval is not None:
                execution = await self._repo.get_approval_execution_for_approval(
                    int(approval["id"]), guild_id=str(actor.guild_id)
                )
        return draft, version, approval, execution

    def _view(self, actor: ApprovalActor, draft, version, approval, execution) -> ContentDraftView:
        findings = load_findings(version["validation_json"])
        status = self._status(draft, approval, execution)
        approval_state = str(approval["state"]) if approval is not None else None
        execution_state = str(execution["state"]) if execution is not None else None
        errors = any(item.severity is ValidationSeverity.error for item in findings)
        is_human = actor.kind is ActorKind.human
        can_propose = ApprovalPermission.propose in actor.permissions
        can_decide = ApprovalPermission.decide in actor.permissions
        can_execute = ApprovalPermission.execute in actor.permissions
        can_cancel = ApprovalPermission.cancel in actor.permissions
        return ContentDraftView(
            draft_id=int(draft["id"]),
            guild_id=int(draft["guild_id"]),
            target_channel_id=int(draft["target_channel_id"]),
            content_type=ContentType(draft["content_type"]),
            topic=str(draft["topic"]),
            audience=str(draft["audience"]),
            tone=str(draft["tone"]),
            purpose=str(draft["purpose"]),
            author_id=str(version["created_by"]),
            created_at=str(draft["created_at"]),
            updated_at=str(draft["updated_at"]),
            version=int(draft["current_version"]),
            content=str(version["content"]),
            content_hash=str(version["content_hash"]),
            source_kind=str(draft["source_kind"] or "manual"),
            source_ref=str(draft["source_ref"]) if draft["source_ref"] else None,
            findings=findings,
            status=status,
            approval_id=int(approval["id"]) if approval is not None else None,
            approval_state=approval_state,
            approval_expires_at=str(approval["expires_at"]) if approval is not None else None,
            execution_id=int(execution["id"]) if execution is not None else None,
            execution_state=execution_state,
            execution_started_at=(str(execution["started_at"]) if execution is not None else None),
            published_message_id=(
                str(execution["result_ref"]) if execution is not None and execution["result_ref"] else None
            ),
            can_edit=can_propose
            and status
            in (
                DraftStatus.draft,
                DraftStatus.pending,
                DraftStatus.approved,
                DraftStatus.rejected,
                DraftStatus.expired,
                DraftStatus.cancelled,
                DraftStatus.failed,
            ),
            can_submit=can_propose and status is DraftStatus.draft and not errors,
            can_approve=(
                can_decide
                and is_human
                and status is DraftStatus.pending
                and str(version["created_by"]) != actor.actor_id
            ),
            can_reject=can_decide and status is DraftStatus.pending,
            can_cancel=can_cancel and status in (DraftStatus.pending, DraftStatus.approved),
            can_publish=(
                can_execute
                and is_human
                and self._settings.content_publishing_enabled
                and status is DraftStatus.approved
                and not errors
            ),
            can_recover=(
                can_execute
                and is_human
                and self._settings.content_publishing_enabled
                and status is DraftStatus.executing
            ),
        )

    async def _invalidate_approval(self, actor: ApprovalActor, draft, approval) -> None:
        state = ApprovalState(str(approval["state"]))
        if state in (ApprovalState.executing, ApprovalState.executed):
            raise ContentDraftConflictError(
                "This draft has an execution in progress or already published; editing is blocked."
            )
        if state in (ApprovalState.pending, ApprovalState.approved):
            system = ApprovalActor(
                actor_id="content-studio",
                guild_id=int(draft["guild_id"]),
                kind=ActorKind.system,
                permissions=frozenset({ApprovalPermission.cancel}),
            )
            try:
                await self._approvals.cancel(int(approval["id"]), actor=system, reason="draft edited")
            except ApprovalExpiredError:
                pass

    async def _cancel_orphan_approval(self, guild_id: int, approval_id: int) -> None:
        system = ApprovalActor(
            actor_id="content-studio",
            guild_id=guild_id,
            kind=ActorKind.system,
            permissions=frozenset({ApprovalPermission.cancel}),
        )
        try:
            await self._approvals.cancel(approval_id, actor=system, reason="draft changed")
        except Exception:  # noqa: BLE001 - the request remains unlinked and cannot publish
            log.warning("Could not cancel orphaned content approval %s", approval_id)

    def _require_current_binding(self, draft, approval, version) -> None:
        if (
            approval["action_type"] != "content.publish"
            or approval["target_type"] != "channel"
            or approval["target_id"] != str(draft["target_channel_id"])
            or approval["content_version"] != str(draft["current_version"])
            or approval["content_hash"] != content_hash(str(version["content"]))
        ):
            raise ApprovalBindingError("The approval is bound to a different draft version.")

    async def _context(self, topic: str, *, guild_id: int) -> list[tuple[str, str]]:
        if self._settings.content_context_top_k <= 0:
            return []
        try:
            rows = await self._repo.search_kb(
                topic,
                guild_id=str(guild_id),
                limit=self._settings.content_context_top_k,
                approved_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - context is optional, generation is not
            log.warning("Content context retrieval failed (%s)", type(exc).__name__)
            return []
        return [(str(row["title"] or ""), str(row["body"] or "")) for row in rows]

    async def _voice(self, guild_id: int) -> VoiceProfile:
        try:
            return await load_voice_profile(self._repo, guild_id)
        except Exception as exc:  # noqa: BLE001 - voice is optional style guidance
            log.warning("Voice profile load failed (%s)", type(exc).__name__)
            return VoiceProfile()

    async def _feedback(self, guild_id: int) -> list[str]:
        limit = int(self._settings.content_feedback_limit)
        if limit <= 0:
            return []
        try:
            rows = await self._repo.list_content_feedback(guild_id=str(guild_id), limit=limit)
        except Exception as exc:  # noqa: BLE001 - feedback is optional style guidance
            log.warning("Content feedback load failed (%s)", type(exc).__name__)
            return []
        return recent_feedback_prompt(rows, limit_chars=1_200)

    async def _approved_examples(self, guild_id: int) -> list[str]:
        if self._settings.content_context_max_chars <= 0:
            return []
        try:
            rows = await self._repo.recent_approved_content(guild_id=str(guild_id), limit=3)
        except Exception as exc:  # noqa: BLE001 - examples are optional style guidance
            log.warning("Approved content example load failed (%s)", type(exc).__name__)
            return []
        return [str(row["content"] or "") for row in rows if str(row["content"] or "").strip()]

    async def _near_duplicate_check(
        self, content: str, *, guild_id: int, exclude_draft_id: int | None
    ) -> ValidationFinding | None:
        threshold = float(self._settings.content_similarity_threshold)
        limit = int(self._settings.content_dedupe_recent)
        if threshold <= 0 or limit <= 0:
            return None
        try:
            rows = await self._repo.recent_approved_content(guild_id=str(guild_id), limit=limit)
        except Exception as exc:  # noqa: BLE001 - advisory check only, exact hash still applies
            log.warning("Near-duplicate check failed (%s)", type(exc).__name__)
            return None
        best_ratio = 0.0
        best_row = None
        for row in rows:
            if exclude_draft_id is not None and int(row["draft_id"]) == int(exclude_draft_id):
                continue
            ratio = similarity_ratio(content, str(row["content"] or ""))
            if ratio >= threshold and ratio > best_ratio:
                best_ratio = ratio
                best_row = row
        if best_row is None:
            return None
        return ValidationFinding(
            code="near_duplicate_content",
            message=(
                f"This draft closely repeats draft {int(best_row['draft_id'])} "
                f"v{int(best_row['version'])} ({round(best_ratio * 100)}% word overlap). "
                "Revise it or confirm the repetition is intentional before submitting."
            ),
            severity=ValidationSeverity.warning,
        )

    def _validate_target(
        self,
        actor: ApprovalActor,
        target: ContentTarget,
        *,
        expected_channel_id: int | None = None,
    ) -> None:
        if actor.guild_id not in set(self._settings.guild_allowlist):
            raise ApprovalDeniedError()
        if self._settings.channel_allowlist and target.channel_id not in set(self._settings.channel_allowlist):
            raise ContentTargetError("The target channel is outside the configured allowlist.")
        if target.guild_id != actor.guild_id:
            raise ApprovalDeniedError()
        if expected_channel_id is not None and target.channel_id != expected_channel_id:
            raise ContentTargetError("The target channel does not match this draft.")
        if not target.exists or not target.can_send:
            raise ContentTargetError()

    def _validate_content(
        self,
        content: str,
        *,
        guild_id: int,
        exclude_draft_id: int | None = None,
        extra: Sequence[ValidationFinding] = (),
    ) -> list[ValidationFinding]:
        findings = list(extra)
        if not content.strip():
            findings.append(ValidationFinding(code="content_empty", message="Draft content is empty."))
        if len(content) > self._settings.content_generation_max_chars:
            findings.append(
                ValidationFinding(
                    code="content_too_long",
                    message=(
                        f"Draft is {len(content)} characters; the limit is "
                        f"{self._settings.content_generation_max_chars}."
                    ),
                )
            )
        if any(ord(char) < 32 and char not in "\n\t" for char in content):
            findings.append(
                ValidationFinding(
                    code="control_character",
                    message="Draft contains a control character that Discord cannot render safely.",
                )
            )
        lowered = content.lower()
        if "@everyone" in lowered or "@here" in lowered:
            findings.append(
                ValidationFinding(
                    code="mass_mention",
                    message="Draft contains @everyone or @here; remove the mass mention.",
                )
            )
        return findings[:MAX_FINDINGS]

    def _status(self, draft, approval, execution) -> DraftStatus:
        if str(draft["state"]) == "published":
            return DraftStatus.published
        if approval is None:
            state = str(draft["state"])
            if state == "failed":
                return DraftStatus.failed
            return DraftStatus.draft
        try:
            state = ApprovalState(str(approval["state"]))
        except ValueError:  # pragma: no cover - schema prevents this
            return DraftStatus.failed
        return {
            ApprovalState.pending: DraftStatus.pending,
            ApprovalState.approved: DraftStatus.approved,
            ApprovalState.rejected: DraftStatus.rejected,
            ApprovalState.expired: DraftStatus.expired,
            ApprovalState.cancelled: DraftStatus.cancelled,
            ApprovalState.executing: DraftStatus.executing,
            ApprovalState.executed: DraftStatus.published,
            ApprovalState.failed: DraftStatus.failed,
        }[state]

    @staticmethod
    def _require(actor: ApprovalActor, permission: ApprovalPermission) -> None:
        if permission not in actor.permissions:
            raise ApprovalDeniedError()

    @staticmethod
    def _require_read(actor: ApprovalActor) -> None:
        readable = {
            ApprovalPermission.propose,
            ApprovalPermission.decide,
            ApprovalPermission.execute,
        }
        if not (readable & set(actor.permissions)):
            raise ApprovalDeniedError()

    async def _duplicate_check(
        self, content: str, *, guild_id: int, exclude_draft_id: int | None
    ) -> ValidationFinding | None:
        duplicate = await self._repo.find_content_version_by_hash(
            guild_id=str(guild_id),
            content_hash=content_hash(content),
            exclude_draft_id=exclude_draft_id,
        )
        if duplicate is None:
            return None
        return _duplicate_finding(duplicate)


def _duplicate_finding(duplicate) -> ValidationFinding:
    return ValidationFinding(
        code="duplicate_content",
        message=(
            f"An identical draft version already exists (draft {duplicate['draft_id']}, "
            f"version {duplicate['version']})."
        ),
        severity=ValidationSeverity.warning,
    )


def _findings_detail(findings: Sequence[ValidationFinding]) -> str:
    return "; ".join(f"{item.code}: {item.message}" for item in findings[:10])[:1000]


def _dump_findings(findings: Sequence[ValidationFinding]) -> str:
    return json.dumps([item.model_dump(mode="json") for item in findings])


def load_findings(raw: str) -> list[ValidationFinding]:
    """Fail closed when stored validation data is unreadable."""
    try:
        data = json.loads(raw or "[]")
        if not isinstance(data, list):
            raise ValueError("validation findings must be a list")
        return [ValidationFinding.model_validate(item) for item in data]
    except Exception:  # noqa: BLE001 - corrupt metadata must block approval
        return [
            ValidationFinding(
                code="validation_unavailable",
                message="Stored validation results are unreadable; this version cannot be approved.",
            )
        ]


def _bounded_context(context: Sequence[tuple[str, str]], max_chars: int) -> list[tuple[str, str]]:
    if max_chars <= 0:
        return []
    out: list[tuple[str, str]] = []
    used = 0
    for title, body in context:
        remaining = max_chars - used
        if remaining <= 0:
            break
        clipped = body[:remaining]
        if not clipped.strip():
            continue
        out.append((title, clipped))
        used += len(clipped)
    return out


def _bounded_examples(examples: Sequence[str], max_chars: int) -> list[str]:
    if max_chars <= 0:
        return []
    budget = min(max_chars, 3_000)
    out: list[str] = []
    used = 0
    for content in examples:
        remaining = budget - used
        if remaining <= 0:
            break
        clipped = content[:remaining]
        if not clipped.strip():
            continue
        out.append(clipped)
        used += len(clipped)
    return out
