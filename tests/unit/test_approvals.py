"""Phase 2A approval state machine: permissions, binding, races, idempotency."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from copilot.core.approvals import (
    ActionBinding,
    ActionRequest,
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
    ApprovalService,
    ApprovalState,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionState,
)
from copilot.db.repository import Repository
from copilot.db.retention import purge_thread
from copilot.errors import (
    ApprovalBindingError,
    ApprovalConflictError,
    ApprovalDeniedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalSelfApprovalError,
    DatabaseError,
)

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def actor(
    actor_id: str,
    *,
    guild_id: int = 500,
    kind: ActorKind = ActorKind.human,
    permissions: tuple[ApprovalPermission, ...] = (),
) -> ApprovalActor:
    return ApprovalActor(
        actor_id=actor_id,
        guild_id=guild_id,
        kind=kind,
        permissions=frozenset(permissions),
    )


PROPOSER = actor("agent-1", kind=ActorKind.agent, permissions=(ApprovalPermission.propose,))
DECIDER = actor("mod-1", permissions=(ApprovalPermission.decide,))
CANCELLER = actor("mod-2", permissions=(ApprovalPermission.cancel,))
EXECUTOR = actor("worker-1", kind=ActorKind.system, permissions=(ApprovalPermission.execute,))


def make_service(db) -> tuple[ApprovalService, Clock]:
    clock = Clock()
    return ApprovalService(Repository(db), clock=clock), clock


def make_request(**overrides) -> ActionRequest:
    values = {
        "action_type": "message.publish",
        "guild_id": 500,
        "target_type": "message",
        "target_id": "msg-1",
        "content_version": "v1",
        "content": "hello community",
        "expires_in_s": 3600,
    }
    values.update(overrides)
    return ActionRequest(**values)


def make_execution(approval_id: int, *, key: str = "task-0001", **binding_overrides) -> ExecutionRequest:
    values = {
        "action_type": "message.publish",
        "guild_id": 500,
        "target_type": "message",
        "target_id": "msg-1",
        "content_version": "v1",
        "content": "hello community",
    }
    values.update(binding_overrides)
    return ExecutionRequest(
        approval_id=approval_id,
        binding=ActionBinding(**values),
        idempotency_key=key,
    )


async def request_and_approve(service: ApprovalService) -> int:
    requested = await service.request_action(PROPOSER, make_request())
    await service.approve(requested.id, actor=DECIDER, note="looks good")
    return requested.id


async def audit_actions(db) -> list[str]:
    rows = await db.fetchall("SELECT action FROM audit_log WHERE target_type = 'approval' ORDER BY id")
    return [row["action"] for row in rows]


# --------------------------------------------------------------- decisions


async def test_request_and_human_approval_are_audited_without_storing_content(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request(content="TOP SECRET"))

    assert requested.state is ApprovalState.pending
    assert requested.requested_by == "agent-1"
    assert requested.requester_kind is ActorKind.agent
    assert requested.content_hash != "TOP SECRET"
    assert len(requested.content_hash) == 64
    stored = await tmp_db.fetchone("SELECT * FROM approval_requests WHERE id = ?", (requested.id,))
    audit = await tmp_db.fetchone("SELECT * FROM audit_log WHERE action = 'approval.requested'")
    assert "TOP SECRET" not in str(dict(stored))
    assert "TOP SECRET" not in str(dict(audit))

    approved = await service.approve(requested.id, actor=DECIDER, note="ship it")
    assert approved.state is ApprovalState.approved
    assert approved.decided_by == "mod-1"
    assert approved.decision_note == "ship it"
    assert approved.decided_at == NOW

    assert await audit_actions(tmp_db) == ["approval.requested", "approval.approved"]


async def test_human_rejection_is_terminal_and_audited(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request())

    rejected = await service.reject(requested.id, actor=DECIDER, note="unsafe")
    assert rejected.state is ApprovalState.rejected
    assert rejected.decided_by == "mod-1"
    with pytest.raises(ApprovalConflictError):
        await service.approve(requested.id, actor=DECIDER)
    assert await audit_actions(tmp_db) == ["approval.requested", "approval.rejected"]
    with pytest.raises(ApprovalConflictError):
        await service.begin_execution(make_execution(requested.id), actor=EXECUTOR)


async def test_missing_request_is_not_found_for_decision_and_execution(tmp_db):
    service, _ = make_service(tmp_db)
    with pytest.raises(ApprovalNotFoundError):
        await service.approve(404, actor=DECIDER)
    with pytest.raises(ApprovalNotFoundError):
        await service.begin_execution(make_execution(404), actor=EXECUTOR)


async def test_expired_request_is_marked_expired_and_cannot_be_approved(tmp_db):
    service, clock = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request(expires_in_s=10))
    clock.advance(11)

    with pytest.raises(ApprovalExpiredError):
        await service.approve(requested.id, actor=DECIDER)
    row = await tmp_db.fetchone("SELECT state FROM approval_requests WHERE id = ?", (requested.id,))
    assert row["state"] == "expired"
    assert await audit_actions(tmp_db) == ["approval.requested", "approval.expired"]


async def test_expire_due_sweeps_pending_and_approved_requests(tmp_db):
    service, clock = make_service(tmp_db)
    pending = await service.request_action(PROPOSER, make_request(expires_in_s=10))
    approved = await service.request_action(PROPOSER, make_request(target_id="msg-2", expires_in_s=10))
    await service.approve(approved.id, actor=DECIDER)
    clock.advance(11)

    assert await service.expire_due() == 2
    rows = await tmp_db.fetchall("SELECT id, state FROM approval_requests ORDER BY id")
    assert [(row["id"], row["state"]) for row in rows] == [
        (pending.id, "expired"),
        (approved.id, "expired"),
    ]
    assert await service.expire_due() == 0


# ------------------------------------------------------------- authorization


async def test_non_human_or_unpermitted_approver_is_denied(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request())

    with pytest.raises(ApprovalDeniedError):
        await service.approve(
            requested.id,
            actor=actor(
                "agent-2",
                kind=ActorKind.agent,
                permissions=(ApprovalPermission.decide,),
            ),
        )
    with pytest.raises(ApprovalDeniedError):
        await service.approve(requested.id, actor=actor("human-no-decide"))
    assert (await service.get_request(requested.id, actor=PROPOSER)).state is ApprovalState.pending
    assert "approval.decision_denied" in await audit_actions(tmp_db)


async def test_requester_cannot_approve_their_own_action(tmp_db):
    service, _ = make_service(tmp_db)
    requester = actor(
        "human-1",
        permissions=(ApprovalPermission.propose, ApprovalPermission.decide),
    )
    requested = await service.request_action(requester, make_request())

    with pytest.raises(ApprovalSelfApprovalError):
        await service.approve(requested.id, actor=requester)
    assert (await service.get_request(requested.id, actor=requester)).state is ApprovalState.pending
    assert "approval.decision_denied" in await audit_actions(tmp_db)


async def test_wrong_guild_is_reported_without_cross_guild_access(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request())

    other_guild_decider = actor("mod-999", guild_id=999, permissions=(ApprovalPermission.decide,))
    with pytest.raises(ApprovalNotFoundError):
        await service.approve(requested.id, actor=other_guild_decider)
    with pytest.raises(ApprovalNotFoundError):
        await service.get_request(requested.id, actor=other_guild_decider)
    assert (await service.get_request(requested.id, actor=PROPOSER)).state is ApprovalState.pending


# ---------------------------------------------------------------- binding


async def test_changed_target_action_version_or_content_blocks_execution(tmp_db):
    service, _ = make_service(tmp_db)
    approval_id = await request_and_approve(service)

    for override in (
        {"target_id": "msg-2"},
        {"action_type": "message.delete"},
        {"content_version": "v2"},
        {"content": "different text"},
    ):
        with pytest.raises(ApprovalBindingError):
            await service.begin_execution(make_execution(approval_id, **override), actor=EXECUTOR)

    assert (await service.get_request(approval_id, actor=PROPOSER)).state is ApprovalState.approved
    assert (await tmp_db.counts())["approval_executions"] == 0
    assert "approval.execution_denied" in await audit_actions(tmp_db)


async def test_execution_revalidates_expiry_after_approval(tmp_db):
    service, clock = make_service(tmp_db)
    approval_id = await request_and_approve(service)
    clock.advance(3601)

    with pytest.raises(ApprovalExpiredError):
        await service.begin_execution(make_execution(approval_id), actor=EXECUTOR)
    row = await tmp_db.fetchone("SELECT state FROM approval_requests WHERE id = ?", (approval_id,))
    assert row["state"] == "expired"
    assert (await tmp_db.counts())["approval_executions"] == 0


# ------------------------------------------------------------ cancel/state


async def test_cancellation_blocks_later_decision_and_execution(tmp_db):
    service, _ = make_service(tmp_db)
    pending = await service.request_action(PROPOSER, make_request())
    cancelled = await service.cancel(pending.id, actor=CANCELLER, reason="withdrawn")
    assert cancelled.state is ApprovalState.cancelled
    assert cancelled.cancelled_by == "mod-2"
    with pytest.raises(ApprovalConflictError):
        await service.approve(pending.id, actor=DECIDER)

    approved_id = await request_and_approve(service)
    await service.cancel(approved_id, actor=CANCELLER)
    with pytest.raises(ApprovalConflictError):
        await service.begin_execution(make_execution(approved_id), actor=EXECUTOR)
    assert "approval.cancelled" in await audit_actions(tmp_db)


async def test_invalid_state_transitions_fail_closed(tmp_db):
    service, _ = make_service(tmp_db)
    approval_id = await request_and_approve(service)

    with pytest.raises(ApprovalConflictError):
        await service.reject(approval_id, actor=DECIDER)
    with pytest.raises(ApprovalNotFoundError):
        await service.finish_execution(999, actor=EXECUTOR, outcome=ExecutionOutcome.executed)


async def test_concurrent_approve_and_reject_have_one_winner(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request())

    results = await asyncio.gather(
        service.approve(requested.id, actor=DECIDER),
        service.reject(requested.id, actor=DECIDER),
        return_exceptions=True,
    )
    successes = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [result for result in results if isinstance(result, ApprovalConflictError)]

    assert len(successes) == 1
    assert len(conflicts) == 1
    state = (await service.get_request(requested.id, actor=PROPOSER)).state
    assert state in (ApprovalState.approved, ApprovalState.rejected)
    assert len([action for action in await audit_actions(tmp_db) if "decision_denied" in action]) == 0


async def test_concurrent_duplicate_approvals_only_create_one_approval(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request())

    results = await asyncio.gather(
        service.approve(requested.id, actor=DECIDER),
        service.approve(requested.id, actor=DECIDER),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, ApprovalConflictError) for result in results) == 1
    assert (await audit_actions(tmp_db)).count("approval.approved") == 1


# --------------------------------------------------------------- execution


async def test_duplicate_execution_attempts_are_idempotent(tmp_db):
    service, _ = make_service(tmp_db)
    approval_id = await request_and_approve(service)

    first = await service.begin_execution(make_execution(approval_id), actor=EXECUTOR)
    second = await service.begin_execution(make_execution(approval_id), actor=EXECUTOR)
    assert first.id == second.id
    assert first.state is ExecutionState.executing
    assert (await tmp_db.counts())["approval_executions"] == 1

    finished = await service.finish_execution(first.id, actor=EXECUTOR, outcome=ExecutionOutcome.executed)
    assert finished.state is ExecutionState.executed
    replay = await service.begin_execution(make_execution(approval_id), actor=EXECUTOR)
    assert replay.id == first.id
    assert replay.state is ExecutionState.executed
    assert (await tmp_db.counts())["approval_executions"] == 1

    with pytest.raises(ApprovalConflictError):
        await service.begin_execution(make_execution(approval_id, key="task-0002"), actor=EXECUTOR)

    repeated_finish = await service.finish_execution(first.id, actor=EXECUTOR, outcome=ExecutionOutcome.executed)
    assert repeated_finish.state is ExecutionState.executed
    assert (await audit_actions(tmp_db)).count("approval.executed") == 1


async def test_idempotency_key_cannot_be_reused_for_another_approval(tmp_db):
    service, _ = make_service(tmp_db)
    first_id = await request_and_approve(service)
    second_id = await request_and_approve(service)
    await service.begin_execution(make_execution(first_id, key="shared-key-1"), actor=EXECUTOR)

    with pytest.raises(ApprovalConflictError):
        await service.begin_execution(make_execution(second_id, key="shared-key-1"), actor=EXECUTOR)


async def test_execution_failure_is_terminal_and_audited(tmp_db):
    service, _ = make_service(tmp_db)
    approval_id = await request_and_approve(service)
    attempt = await service.begin_execution(make_execution(approval_id), actor=EXECUTOR)

    failed = await service.finish_execution(
        attempt.id,
        actor=EXECUTOR,
        outcome=ExecutionOutcome.failed,
        error_code="provider_error",
    )
    assert failed.state is ExecutionState.failed
    assert failed.error_code == "provider_error"
    row = await tmp_db.fetchone("SELECT state FROM approval_requests WHERE id = ?", (approval_id,))
    assert row["state"] == "failed"
    assert "approval.execution_failed" in await audit_actions(tmp_db)
    with pytest.raises(ApprovalConflictError):
        await service.begin_execution(make_execution(approval_id, key="task-0002"), actor=EXECUTOR)


async def test_only_the_claiming_executor_can_finish_an_attempt(tmp_db):
    service, _ = make_service(tmp_db)
    approval_id = await request_and_approve(service)
    attempt = await service.begin_execution(make_execution(approval_id), actor=EXECUTOR)
    other = actor("worker-2", kind=ActorKind.system, permissions=(ApprovalPermission.execute,))

    with pytest.raises(ApprovalDeniedError):
        await service.finish_execution(attempt.id, actor=other, outcome=ExecutionOutcome.executed)
    assert (await service.get_execution(attempt.id, actor=EXECUTOR)).state is ExecutionState.executing


async def test_execution_permission_and_guild_scope_fail_closed(tmp_db):
    service, _ = make_service(tmp_db)
    approval_id = await request_and_approve(service)

    no_execute = actor("worker-no-perm", kind=ActorKind.system)
    with pytest.raises(ApprovalDeniedError):
        await service.begin_execution(make_execution(approval_id), actor=no_execute)
    other_guild = actor("worker-999", guild_id=999, kind=ActorKind.system, permissions=(ApprovalPermission.execute,))
    with pytest.raises(ApprovalNotFoundError):
        await service.begin_execution(make_execution(approval_id), actor=other_guild)
    assert (await service.get_request(approval_id, actor=PROPOSER)).state is ApprovalState.approved


# ------------------------------------------------------- persistence/failures


async def test_request_and_audit_are_one_transaction(tmp_db):
    service, _ = make_service(tmp_db)
    await tmp_db.execute(
        "CREATE TRIGGER fail_approval_audit BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'audit failed'); END"
    )
    try:
        with pytest.raises(DatabaseError):
            await service.request_action(PROPOSER, make_request())
        assert (await tmp_db.counts())["approval_requests"] == 0
        assert (await tmp_db.counts())["audit_log"] == 0
    finally:
        await tmp_db.execute("DROP TRIGGER fail_approval_audit")


async def test_decision_and_audit_roll_back_together(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request())
    await tmp_db.execute(
        "CREATE TRIGGER fail_approval_audit BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'audit failed'); END"
    )
    try:
        with pytest.raises(DatabaseError):
            await service.approve(requested.id, actor=DECIDER)
    finally:
        await tmp_db.execute("DROP TRIGGER fail_approval_audit")

    row = await tmp_db.fetchone("SELECT state FROM approval_requests WHERE id = ?", (requested.id,))
    assert row["state"] == "pending"
    assert "approval.approved" not in await audit_actions(tmp_db)


async def test_storage_contention_fails_closed_without_a_decision(tmp_db):
    service, _ = make_service(tmp_db)
    requested = await service.request_action(PROPOSER, make_request())
    await tmp_db.execute("PRAGMA busy_timeout=50")

    holder = sqlite3.connect(tmp_db.path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(DatabaseError):
            await service.approve(requested.id, actor=DECIDER)
    finally:
        holder.rollback()
        holder.close()

    row = await tmp_db.fetchone("SELECT state FROM approval_requests WHERE id = ?", (requested.id,))
    assert row["state"] == "pending"


async def test_approval_records_and_audit_survive_retention_of_unrelated_threads(tmp_db):
    service, _ = make_service(tmp_db)
    repo = Repository(tmp_db)
    thread_id = await repo.upsert_thread(discord_thread_id="1001", guild_id="500", parent_channel_id="42", name="login")
    approval_id = await request_and_approve(service)
    attempt = await service.begin_execution(make_execution(approval_id), actor=EXECUTOR)
    await service.finish_execution(attempt.id, actor=EXECUTOR, outcome=ExecutionOutcome.executed)

    await purge_thread(tmp_db, "1001", guild_id="500")
    counts = await tmp_db.counts()
    assert counts["approval_requests"] == 1
    assert counts["approval_executions"] == 1
    assert counts["audit_log"] == 4
    assert (await repo.get_thread_row(thread_id)) is None
