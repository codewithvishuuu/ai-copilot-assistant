"""Explicit purge of one approved knowledge-base document.

Approved knowledge is normally retained deliberately (including after its source
thread expires). This service is the human-authorized exception: a moderator
names one document id in the caller's guild, and the repository deletes it and
writes the audit row in one transaction. No title, URL, or body text is copied
into the audit record.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..db.repository import Repository
from ..errors import (
    ApprovalDeniedError,
    KnowledgeDocumentNotApprovedError,
    KnowledgeDocumentNotFoundError,
)
from .approvals import ActorKind, ApprovalActor, ApprovalPermission


class PurgedKnowledgeDocument(BaseModel):
    """Minimal facts about a completed purge (no document text)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: int
    kind: str
    ref_id: int | None
    body_chars: int


async def purge_approved_document(
    actor: ApprovalActor, repo: Repository, *, document_id: int
) -> PurgedKnowledgeDocument:
    """Permanently remove one approved KB document after a fresh human check.

    The actor is rebuilt from Discord on each command invocation by the cog;
    this service additionally requires a human actor with ``decide`` and uses
    ``actor.guild_id`` (never a caller-supplied guild) for the lookup.
    """
    if actor.kind is not ActorKind.human or ApprovalPermission.decide not in actor.permissions:
        raise ApprovalDeniedError()

    outcome, facts = await repo.purge_approved_kb_doc(
        int(document_id), guild_id=str(actor.guild_id), actor_id=actor.actor_id
    )
    if outcome == "not_found":
        raise KnowledgeDocumentNotFoundError()
    if outcome == "not_approved":
        raise KnowledgeDocumentNotApprovedError()

    return PurgedKnowledgeDocument(
        document_id=int(facts["document_id"]),
        kind=str(facts["kind"]),
        ref_id=int(facts["ref_id"]) if facts["ref_id"] is not None else None,
        body_chars=int(facts["body_chars"]),
    )
