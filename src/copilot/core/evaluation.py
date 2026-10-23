"""Offline agent evaluation: contract, safety, isolation and regression checks.

These checks are deterministic and local: they use temporary databases, the
registered read-only skills and pure validation helpers. They do **not** measure
model answer quality, and the report says so explicitly. A real quality
evaluation would require a labelled dataset and live inference; that is deferred
to live E2E work and is never claimed here.
"""

from __future__ import annotations

import base64
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..agents.contract import (
    MAX_SKILL_OUTPUT_CHARS,
    MAX_SKILL_TIMEOUT_S,
    Skill,
    SkillContext,
    SkillPermission,
    SkillStatus,
    TaskRequest,
)
from ..agents.host import AgentRunService, HostPlanner
from ..agents.orchestrator import SkillOrchestrator
from ..agents.roles import RoleRegistry
from ..db.database import Database
from ..db.repository import Repository
from ..errors import (
    AgentRunNotFoundError,
    ApprovalSelfApprovalError,
    ScheduledJobNotFoundError,
)
from ..util.logging import redact
from .approvals import (
    ActionRequest,
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
    ApprovalService,
)
from .content_studio import content_hash
from .images import prepare_image_bytes, sniff_image_format
from .scheduler import ScheduledJobKind, SchedulerService

MIN_SCHEMA_VERSION = 9
_REQUIRED_TABLES = {
    "missions",
    "mission_steps",
    "research_reports",
    "content_calendar_entries",
    "contextual_replies",
    "agent_eval_runs",
    "agent_runs",
    "agent_run_steps",
    "attachments",
    "guild_voice_profiles",
    "content_feedback",
    "moderation_policies",
    "moderation_incidents",
    "moderation_warnings",
    "scheduled_jobs",
    "reply_approvals",
}
_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_DISCLAIMER = (
    "Offline contract, safety, isolation and migration checks only. No model output quality "
    "was evaluated, no Discord or web provider was contacted, and no production data was used."
)


class EvaluationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=3, max_length=64)
    category: Literal["contract", "safety", "isolation", "regression"]
    title: str = Field(min_length=3, max_length=160)
    passed: bool
    detail: str = Field(default="", max_length=400)


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: int | None = None
    guild_id: str
    requested_by: str
    created_at: str
    checks: list[EvaluationCheck] = Field(default_factory=list)
    passed: bool = False
    passed_count: int = 0
    failed_count: int = 0
    offline_only: bool = True
    model_quality_evaluated: bool = False
    summary: str = ""
    disclaimer: str = _DISCLAIMER


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _check(
    check_id: str,
    category: Literal["contract", "safety", "isolation", "regression"],
    title: str,
    fn: Callable[[], str],
) -> EvaluationCheck:
    try:
        detail = fn()
        return EvaluationCheck(
            check_id=check_id,
            category=category,
            title=title,
            passed=True,
            detail=str(detail)[:400],
        )
    except Exception as exc:  # noqa: BLE001 - a failed check is data, not a crash
        return EvaluationCheck(
            check_id=check_id,
            category=category,
            title=title,
            passed=False,
            detail=f"{type(exc).__name__}: {exc}"[:400],
        )


class EvaluationService:
    """Run the offline suite and persist one reproducible report."""

    def __init__(
        self,
        repo: Repository,
        orchestrator: SkillOrchestrator,
        settings: Any,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._orchestrator = orchestrator
        self._settings = settings
        self._clock = clock

    async def run(self, *, guild_id: str, requested_by: str) -> EvaluationReport:
        checks: list[EvaluationCheck] = []
        checks.extend(self._registry_checks())
        checks.extend(self._agent_contract_checks())
        checks.append(await self._invalid_input_check())
        checks.append(await self._unknown_skill_check())
        checks.append(await self._permission_check())
        checks.append(await self._guild_scope_check())
        checks.append(await self._channel_scope_check())
        checks.append(self._redaction_check())
        checks.append(self._image_check())
        try:
            checks.extend(await self._temporary_database_checks())
        except Exception as exc:  # noqa: BLE001 - harness failure must be reported, not raised
            checks.append(
                EvaluationCheck(
                    check_id="harness.temp_database",
                    category="regression",
                    title="Temporary evaluation database",
                    passed=False,
                    detail=f"The isolated evaluation database could not run ({type(exc).__name__}).",
                )
            )

        passed_count = sum(1 for check in checks if check.passed)
        failed_count = len(checks) - passed_count
        report = EvaluationReport(
            guild_id=str(guild_id),
            requested_by=str(requested_by),
            created_at=self._clock().isoformat(timespec="seconds"),
            checks=checks,
            passed=failed_count == 0,
            passed_count=passed_count,
            failed_count=failed_count,
            summary=(
                f"{passed_count}/{len(checks)} offline checks passed."
                if failed_count
                else f"All {len(checks)} offline checks passed."
            ),
        )
        status = "completed" if report.passed else "failed"
        evaluation_id = await self._repo.insert_agent_eval_run(
            guild_id=str(guild_id),
            requested_by=str(requested_by),
            status=status,
            report_json=report.model_dump_json(),
            created_at=report.created_at,
        )
        await self._repo.audit(
            actor_id=str(requested_by),
            action="evaluation.completed",
            target_type="agent_eval_run",
            target_id=str(evaluation_id),
            guild_id=str(guild_id),
            meta={
                "guild_id": str(guild_id),
                "passed": report.passed,
                "checks": len(checks),
                "failed": failed_count,
            },
        )
        return report.model_copy(update={"evaluation_id": evaluation_id})

    # ------------------------------------------------------------ contract checks

    def _registry_checks(self) -> list[EvaluationCheck]:
        skills = self._orchestrator.registry.all()

        def registry() -> str:
            ids: set[str] = set()
            for skill in skills:
                assert isinstance(skill, Skill), "registry contains a non-Skill entry"
                assert skill.read_only is True, f"{skill.skill_id} is not read-only"
                assert skill.permissions, f"{skill.skill_id} declares no permissions"
                assert 0 < skill.limits.timeout_s <= MAX_SKILL_TIMEOUT_S, f"{skill.skill_id} timeout"
                assert 0 < skill.limits.max_output_chars <= MAX_SKILL_OUTPUT_CHARS, (
                    f"{skill.skill_id} output cap"
                )
                assert issubclass(skill.input_model, BaseModel), f"{skill.skill_id} input model"
                assert skill.skill_id not in ids, f"duplicate skill id {skill.skill_id}"
                ids.add(skill.skill_id)
            return f"{len(skills)} read-only skill(s) validated."

        return [_check("skill.registry", "contract", "Skill registry contract", registry)]

    def _agent_contract_checks(self) -> list[EvaluationCheck]:
        registered = frozenset(self._orchestrator.registry.ids)

        def roles() -> str:
            registry = RoleRegistry()
            roster = registry.all()
            assert len(roster) == 8, f"expected 8 specialist roles, found {len(roster)}"
            availability = registry.available(registered)
            assert set(availability) == {role.role_id for role in roster}
            for role in roster:
                assert role.capabilities, f"{role.role_id.value} declares no capabilities"
                if role.writes_public_content:
                    assert role.requires_human_approval, (
                        f"{role.role_id.value} writes public content without approval"
                    )
            return f"{len(roster)} roles validated; availability matches registered skills."

        def plan() -> str:
            planner = HostPlanner(self._settings)
            planned = planner.plan("summarize community health for the week", registered)
            seen: set[int] = set()
            for task in planned.tasks:
                assert task.skill_id in registered, f"{task.skill_id} is not registered"
                assert all(
                    dependency in seen and dependency < task.index
                    for dependency in task.depends_on
                ), f"task {task.index} has a forward or missing dependency"
                seen.add(task.index)
            if "synthesis.brief" in registered:
                assert planned.tasks[-1].skill_id == "synthesis.brief", "no synthesis step"
                assert set(planned.tasks[-1].depends_on) == set(range(len(planned.tasks) - 1))
            else:
                assert any("combination is unavailable" in note for note in planned.unavailable)
            return f"{len(planned.tasks)} dependency-ordered, registered-skill task(s) planned."

        def honesty() -> str:
            if "research.web" in registered:
                return "A web research provider is registered."
            planned = HostPlanner(self._settings).plan(
                "research the latest release notes", registered
            )
            assert any("Web Researcher is unavailable" in note for note in planned.unavailable), (
                "missing web provider was not reported honestly"
            )
            assert all(task.skill_id != "research.web" for task in planned.tasks)
            return "A missing web provider is reported instead of being simulated."

        return [
            _check("agent.roles", "contract", "Specialist role roster", roles),
            _check("agent.plan", "contract", "Bounded dependency-aware planning", plan),
            _check("agent.provider_honesty", "safety", "Provider availability honesty", honesty),
        ]

    async def _invalid_input_check(self) -> EvaluationCheck:
        orchestrator = self._evaluation_orchestrator()
        context = self._evaluation_context(permissions=self._all_permissions())

        async def run() -> str:
            for skill_id in orchestrator.registry.ids:
                result = await orchestrator.run(
                    TaskRequest(skill_id=skill_id, input={"unexpected_field": "x"}), context
                )
                assert result.status is SkillStatus.invalid_input, (
                    f"{skill_id} accepted an invalid payload ({result.status.value})"
                )
            return f"All {len(orchestrator.registry.ids)} skill(s) rejected an invalid payload."

        return await self._async_check(
            "skill.invalid_input", "contract", "Skills reject invalid input", run
        )

    async def _unknown_skill_check(self) -> EvaluationCheck:
        orchestrator = self._evaluation_orchestrator()
        context = self._evaluation_context(permissions=self._all_permissions())

        async def run() -> str:
            result = await orchestrator.run(
                TaskRequest(skill_id="not.a.skill", input={}), context
            )
            assert result.status is SkillStatus.unknown_skill, result.status.value
            return "Unknown skill ids are rejected without dispatch."

        return await self._async_check(
            "skill.unknown", "contract", "Unknown skills are not dispatched", run
        )

    async def _permission_check(self) -> EvaluationCheck:
        orchestrator = self._evaluation_orchestrator()
        moderator_skills = [
            skill
            for skill in orchestrator.registry.all()
            if SkillPermission.moderator in skill.permissions
        ]
        context = self._evaluation_context(permissions=frozenset({SkillPermission.guild_member}))

        async def run() -> str:
            if not moderator_skills:
                return "No moderator-only skills are registered; nothing to deny."
            for skill in moderator_skills:
                result = await orchestrator.run(
                    TaskRequest(skill_id=skill.skill_id, input={}), context
                )
                assert result.status is SkillStatus.denied, (
                    f"{skill.skill_id} was reachable with guild_member only"
                )
            return f"{len(moderator_skills)} moderator skill(s) denied guild_member callers."

        return await self._async_check(
            "skill.permission", "safety", "Skill permissions are enforced", run
        )

    async def _guild_scope_check(self) -> EvaluationCheck:
        orchestrator = self._evaluation_orchestrator()

        async def run() -> str:
            context = self._evaluation_context(permissions=self._all_permissions(), guild_id=999)
            result = await orchestrator.run(
                TaskRequest(skill_id="kb.search", input={"query": "anything", "limit": 1}), context
            )
            assert result.status is SkillStatus.denied, result.status.value
            return "A guild outside the allowlist is denied before skill dispatch."

        return await self._async_check(
            "scope.guild", "isolation", "Guild allowlist is enforced", run
        )

    async def _channel_scope_check(self) -> EvaluationCheck:
        orchestrator = SkillOrchestrator(
            self._orchestrator.registry,
            allowed_guild_ids=[1],
            allowed_channel_ids=[77],
        )

        async def run() -> str:
            context = SkillContext(
                guild_id=1,
                channel_id=88,
                parent_id=None,
                user_id=1,
                permissions=self._all_permissions(),
            )
            result = await orchestrator.run(
                TaskRequest(skill_id="kb.search", input={"query": "anything", "limit": 1}), context
            )
            assert result.status is SkillStatus.denied, result.status.value
            return "A channel outside the allowlist is denied before skill dispatch."

        return await self._async_check(
            "scope.channel", "isolation", "Channel allowlist is enforced", run
        )

    def _redaction_check(self) -> EvaluationCheck:
        def run() -> str:
            sample = "api_key=super-secret-value-123 token: abcdef"
            cleaned = redact(sample)
            assert "super-secret-value-123" not in cleaned, "API key was not redacted"
            assert "abcdef" not in cleaned, "token was not redacted"
            return "Secret-shaped values are redacted from log text."

        return _check("logging.redaction", "safety", "Log redaction", run)

    def _image_check(self) -> EvaluationCheck:
        def run() -> str:
            assert sniff_image_format(b"not an image") is None
            try:
                prepare_image_bytes(b"not an image")
            except ValueError:
                pass
            else:
                raise AssertionError("invalid image bytes were accepted")
            sanitized, _note = prepare_image_bytes(_ONE_PIXEL_PNG)
            assert sniff_image_format(sanitized) == "PNG", "sanitized image is not a PNG"
            return "Invalid image bytes are rejected; valid images are re-encoded."

        return _check("image.validation", "safety", "Image validation and re-encoding", run)

    # ---------------------------------------------------------- isolated database

    async def _temporary_database_checks(self) -> list[EvaluationCheck]:
        checks: list[EvaluationCheck] = []
        with tempfile.TemporaryDirectory(prefix="copilot-eval-") as directory:
            db = Database(Path(directory) / "evaluation.db")
            await db.connect()
            try:
                repo = Repository(db)
                checks.append(await self._migration_check(db))
                checks.extend(await self._database_isolation_checks(repo))
                checks.append(await self._approval_check(repo))
                checks.append(await self._agent_run_check(repo))
                checks.extend(await self._scheduler_checks(repo))
            finally:
                await db.close()
        return checks

    async def _migration_check(self, db: Database) -> EvaluationCheck:
        async def run() -> str:
            version = await db.user_version()
            counts = await db.counts()
            assert version >= MIN_SCHEMA_VERSION, (
                f"schema version {version} is below {MIN_SCHEMA_VERSION}"
            )
            missing = _REQUIRED_TABLES - set(counts)
            assert not missing, f"missing tables: {', '.join(sorted(missing))}"
            return f"Fresh database migrated to v{version}; all required tables exist."

        return await self._async_check(
            "db.migrations", "regression", "Fresh database migrations", run
        )

    async def _database_isolation_checks(self, repo: Repository) -> list[EvaluationCheck]:
        a = await repo.insert_mission_with_steps(
            guild_id="1",
            channel_id="10",
            parent_channel_id=None,
            requester_id="eval-a",
            goal="guild one mission",
            mode="community",
            trace_id="trace-a",
            idempotency_key=None,
            created_at="2026-01-01T00:00:00+00:00",
            plan_json="[]",
            steps=[(0, "Pulse", "pulse.community", "{}")],
        )
        b = await repo.insert_mission_with_steps(
            guild_id="2",
            channel_id="20",
            parent_channel_id=None,
            requester_id="eval-b",
            goal="guild two mission",
            mode="community",
            trace_id="trace-b",
            idempotency_key=None,
            created_at="2026-01-01T00:00:00+00:00",
            plan_json="[]",
            steps=[(0, "Pulse", "pulse.community", "{}")],
        )
        await repo.insert_research_report(
            guild_id="1",
            mission_id=a,
            query="guild one research",
            provider="fake",
            status="completed",
            summary="one",
            synthesis="extractive",
            sources_json="[]",
            error_code=None,
            created_at="2026-01-01T00:00:00+00:00",
        )
        await repo.insert_research_report(
            guild_id="2",
            mission_id=b,
            query="guild two research",
            provider="fake",
            status="completed",
            summary="two",
            synthesis="extractive",
            sources_json="[]",
            error_code=None,
            created_at="2026-01-01T00:00:00+00:00",
        )
        await repo.audit(actor_id="eval", action="eval.guild_one", guild_id="1")
        await repo.audit(actor_id="eval", action="eval.guild_two", guild_id="2")
        await repo.insert_content_draft_with_version(
            guild_id="1",
            target_channel_id="10",
            content_type="educational_post",
            topic="provenance",
            audience="",
            tone="",
            purpose="",
            created_by="eval",
            created_at="2026-01-01T00:00:00+00:00",
            content="eval content",
            content_hash=content_hash("eval content"),
            validation_json="[]",
            source_kind="research",
            source_ref="research:1",
        )

        async def missions() -> str:
            assert await repo.get_mission(b, guild_id="1") is None
            assert await repo.get_mission(a, guild_id="2") is None
            rows = await repo.list_missions(guild_id="1")
            assert [int(row["id"]) for row in rows] == [a]
            assert await repo.list_mission_steps(b, guild_id="1") == []
            return "Missions and steps are invisible across guilds."

        async def research() -> str:
            rows = await repo.list_research_reports(guild_id="1")
            assert len(rows) == 1 and str(rows[0]["query"]) == "guild one research"
            assert all("guild two" not in str(row["query"]) for row in rows)
            return "Research reports are guild-scoped."

        async def audit() -> str:
            rows = await repo.recent_audit(guild_id="1", limit=20)
            actions = {str(row["action"]) for row in rows}
            assert "eval.guild_one" in actions
            assert "eval.guild_two" not in actions
            return "Audit reads never cross guild boundaries."

        async def provenance() -> str:
            rows = await repo.list_content_drafts(guild_id="1")
            assert len(rows) == 1
            assert str(rows[0]["source_kind"]) == "research"
            assert str(rows[0]["source_ref"]) == "research:1"
            assert await repo.list_content_drafts(guild_id="2") == []
            return "Draft provenance persists and remains guild-scoped."

        return [
            await self._async_check("db.guild_isolation", "isolation", "Guild data isolation", missions),
            await self._async_check("db.research_isolation", "isolation", "Research isolation", research),
            await self._async_check("db.audit_isolation", "isolation", "Audit isolation", audit),
            await self._async_check("db.draft_provenance", "regression", "Draft provenance", provenance),
        ]

    async def _agent_run_check(self, repo: Repository) -> EvaluationCheck:
        async def run() -> str:
            service = AgentRunService(
                repo,
                self._evaluation_orchestrator(),
                SimpleNamespace(
                    agent_enabled=True, agent_max_goal_chars=500, agent_max_steps=4
                ),
                clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            )
            context = self._evaluation_context(permissions=self._all_permissions())
            created = await service.create_run(
                context, goal="summarize community health", idempotency_key="eval-run"
            )
            assert created.status.value == "planned", created.status.value
            assert created.steps, "the run has no steps"
            seen: set[int] = set()
            for step in created.steps:
                assert all(
                    dependency in seen and dependency < step.index
                    for dependency in step.depends_on
                ), f"step {step.index} is not dependency-ordered"
                seen.add(step.index)
            repeat = await service.create_run(
                context, goal="summarize community health", idempotency_key="eval-run"
            )
            assert repeat.run_id == created.run_id, "idempotent create made a second run"
            try:
                await service.get(guild_id="2", run_id=created.run_id)
            except AgentRunNotFoundError:
                pass
            else:
                raise AssertionError("agent run was visible across guilds")
            assert await service.list(guild_id="2") == []
            return "Runs are dependency-ordered, idempotent and guild-scoped."

        return await self._async_check(
            "agent.run_isolation",
            "isolation",
            "Agent run persistence and isolation",
            run,
        )

    async def _scheduler_checks(self, repo: Repository) -> list[EvaluationCheck]:
        def kinds() -> str:
            values = {kind.value for kind in ScheduledJobKind}
            assert values == {"agent.run", "content.draft", "digest.collect"}, sorted(values)
            assert not any("publish" in value for value in values), "a publish job kind exists"
            return "Only read-only analysis and approval-gated draft jobs exist; no publish kind."

        async def durable() -> str:
            settings = SimpleNamespace(scheduler_enabled=True, scheduler_max_attempts=3)
            service = SchedulerService(
                repo, settings, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
            )
            context = self._evaluation_context(permissions=self._all_permissions())
            first, created = await service.schedule_agent_run(
                context,
                goal="community health digest",
                run_at="2026-01-02T00:00:00+00:00",
                timezone_name="UTC",
                idempotency_key="eval-job",
            )
            again, created_again = await service.schedule_agent_run(
                context,
                goal="community health digest",
                run_at="2026-01-02T00:00:00+00:00",
                timezone_name="UTC",
                idempotency_key="eval-job",
            )
            assert created is True and created_again is False, "idempotency key was not honored"
            assert again.job_id == first.job_id
            claimed = await repo.claim_scheduled_job(
                first.job_id, now="2026-01-02T00:00:00+00:00"
            )
            assert claimed is True
            assert await service.recover_interrupted() == 1, "interrupted job was not requeued"
            recovered = await service.get_job(guild_id="1", job_id=first.job_id)
            assert recovered.status == "pending", recovered.status
            assert recovered.attempts == 1, recovered.attempts
            try:
                await service.get_job(guild_id="2", job_id=first.job_id)
            except ScheduledJobNotFoundError:
                pass
            else:
                raise AssertionError("scheduled job was visible across guilds")
            cancelled = await service.cancel_job(
                guild_id="1", job_id=first.job_id, actor_id="eval"
            )
            assert cancelled.status == "cancelled", cancelled.status
            return "Jobs are idempotent, restart-recoverable, cancellable and guild-scoped."

        return [
            _check("scheduler.kinds", "safety", "No scheduled publishing kind", kinds),
            await self._async_check(
                "scheduler.durability",
                "regression",
                "Scheduler idempotency and recovery",
                durable,
            ),
        ]

    async def _approval_check(self, repo: Repository) -> EvaluationCheck:
        async def run() -> str:
            service = ApprovalService(repo)
            proposer = ApprovalActor(
                actor_id="eval-proposer",
                guild_id=1,
                kind=ActorKind.human,
                permissions=frozenset(
                    {ApprovalPermission.propose, ApprovalPermission.decide}
                ),
            )
            request = await service.request_action(
                proposer,
                ActionRequest(
                    action_type="content.publish",
                    guild_id=1,
                    target_type="content_draft",
                    target_id="1",
                    content_version="1",
                    content="evaluation content",
                    expires_in_s=300,
                ),
            )
            try:
                await service.approve(request.id, actor=proposer)
            except ApprovalSelfApprovalError:
                pass
            else:
                raise AssertionError("self-approval was accepted")
            other = ApprovalActor(
                actor_id="eval-reviewer",
                guild_id=1,
                kind=ActorKind.human,
                permissions=frozenset({ApprovalPermission.decide}),
            )
            approved = await service.approve(request.id, actor=other)
            assert approved.state.value == "approved", approved.state.value
            return "Self-approval is rejected; a different human moderator can approve."

        return await self._async_check(
            "approval.binding", "safety", "Approval policy and binding", run
        )

    # ----------------------------------------------------------------- helpers

    async def _async_check(
        self,
        check_id: str,
        category: Literal["contract", "safety", "isolation", "regression"],
        title: str,
        fn: Callable[[], Awaitable[str]],
    ) -> EvaluationCheck:
        try:
            detail = await fn()
            return EvaluationCheck(
                check_id=check_id,
                category=category,
                title=title,
                passed=True,
                detail=str(detail)[:400],
            )
        except Exception as exc:  # noqa: BLE001 - a failed check is reported
            return EvaluationCheck(
                check_id=check_id,
                category=category,
                title=title,
                passed=False,
                detail=f"{type(exc).__name__}: {exc}"[:400],
            )

    def _evaluation_orchestrator(self) -> SkillOrchestrator:
        return SkillOrchestrator(
            self._orchestrator.registry,
            allowed_guild_ids=[1],
            allowed_channel_ids=[],
        )

    def _evaluation_context(
        self,
        *,
        permissions: frozenset[SkillPermission],
        guild_id: int = 1,
    ) -> SkillContext:
        return SkillContext(
            guild_id=guild_id,
            channel_id=1,
            parent_id=None,
            user_id=1,
            permissions=permissions,
        )

    @staticmethod
    def _all_permissions() -> frozenset[SkillPermission]:
        return frozenset({SkillPermission.guild_member, SkillPermission.moderator})
