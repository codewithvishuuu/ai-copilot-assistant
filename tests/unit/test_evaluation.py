"""Agent evaluation: offline contract/safety/isolation checks and honest scope."""

from __future__ import annotations

from datetime import UTC, datetime

from fakes import fake_settings
from pydantic import ConfigDict

from copilot.agents.builtin import KbSearchSkill, build_default_registry
from copilot.agents.contract import (
    Skill,
    SkillErrorPolicy,
    SkillInput,
    SkillLimits,
    SkillOutput,
    SkillPermission,
)
from copilot.agents.orchestrator import SkillOrchestrator
from copilot.agents.registry import SkillRegistry
from copilot.core.evaluation import EvaluationReport, EvaluationService
from copilot.db.repository import Repository
from copilot.research.service import ResearchService

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


class _PermissiveInput(SkillInput):
    model_config = ConfigDict(extra="allow")


class _TextOutput(SkillOutput):
    text: str = "ok"


class _PermissiveSkill(Skill[_PermissiveInput, _TextOutput]):
    skill_id = "test.permissive"
    description = "Test-only skill that accepts any payload."
    input_model = _PermissiveInput
    output_model = _TextOutput
    permissions = frozenset({SkillPermission.guild_member})
    limits = SkillLimits(timeout_s=1.0, max_output_chars=1000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    async def execute(self, context, payload):
        return _TextOutput()


def _service(tmp_path, repo: Repository, registry: SkillRegistry) -> EvaluationService:
    settings = fake_settings(tmp_path)
    orchestrator = SkillOrchestrator(registry, allowed_guild_ids=[500], allowed_channel_ids=[])
    return EvaluationService(repo, orchestrator, settings, clock=lambda: NOW)


async def test_offline_evaluation_passes_and_persists_a_guild_scoped_report(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    settings = fake_settings(tmp_path)
    registry = build_default_registry(
        repo,
        insights_window_days=30,
        insights_minimum_threads=5,
        pulse_window_days=30,
        pulse_minimum_threads=5,
        research_service=ResearchService(repo, settings),
    )
    service = _service(tmp_path, repo, registry)

    report = await service.run(guild_id="500", requested_by="7")

    assert report.passed, [(check.check_id, check.detail) for check in report.checks if not check.passed]
    assert report.failed_count == 0
    assert report.passed_count == len(report.checks)
    assert report.offline_only is True
    assert report.model_quality_evaluated is False
    assert "No model output quality" in report.disclaimer
    assert report.evaluation_id is not None

    check_ids = {check.check_id for check in report.checks}
    assert {
        "skill.registry",
        "skill.invalid_input",
        "skill.unknown",
        "skill.permission",
        "scope.guild",
        "scope.channel",
        "logging.redaction",
        "image.validation",
        "db.migrations",
        "db.guild_isolation",
        "db.research_isolation",
        "db.audit_isolation",
        "db.draft_provenance",
        "approval.binding",
    } <= check_ids
    assert {check.category for check in report.checks} == {
        "contract",
        "safety",
        "isolation",
        "regression",
    }

    row = await repo.latest_agent_eval_run(guild_id="500")
    assert row is not None
    stored = EvaluationReport.model_validate_json(str(row["report_json"]))
    assert stored.passed is True
    assert stored.created_at == NOW.isoformat(timespec="seconds")
    audits = await repo.recent_audit(guild_id="500", limit=10)
    assert "evaluation.completed" in {str(item["action"]) for item in audits}
    assert await repo.latest_agent_eval_run(guild_id="999") is None


async def test_evaluation_reports_a_regression_instead_of_claiming_success(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    registry = SkillRegistry()
    registry.register(KbSearchSkill(repo))
    registry.register(_PermissiveSkill())
    service = _service(tmp_path, repo, registry)

    report = await service.run(guild_id="500", requested_by="7")

    assert report.passed is False
    assert report.failed_count == 1
    failed = [check for check in report.checks if not check.passed]
    assert [check.check_id for check in failed] == ["skill.invalid_input"]
    assert "accepted an invalid payload" in failed[0].detail
    assert report.summary == f"{report.passed_count}/{len(report.checks)} offline checks passed."

    row = await repo.latest_agent_eval_run(guild_id="500")
    assert row is not None
    assert str(row["status"]) == "failed"
