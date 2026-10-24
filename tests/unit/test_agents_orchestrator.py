"""Phase 1 orchestrator: auth, bounds, validation, and safe failure mapping."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import Field

from copilot.agents.contract import (
    Reference,
    Skill,
    SkillContext,
    SkillErrorPolicy,
    SkillInput,
    SkillLimits,
    SkillOutput,
    SkillPermission,
    SkillStatus,
    TaskRequest,
)
from copilot.agents.orchestrator import SkillOrchestrator
from copilot.agents.registry import SkillRegistry
from copilot.errors import (
    ProviderError,
    ProviderUnavailableError,
    ResearchNoResultsError,
    ResearchProviderError,
)


class EchoInput(SkillInput):
    text: str = Field(min_length=1, max_length=50)


class EchoOutput(SkillOutput):
    text: str


class EchoSkill(Skill[EchoInput, EchoOutput]):
    skill_id = "test.echo"
    description = "Echo a bounded string."
    input_model = EchoInput
    output_model = EchoOutput
    permissions = frozenset({SkillPermission.guild_member})
    limits = SkillLimits(timeout_s=1.0, max_output_chars=1_000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        return EchoOutput(
            text=payload.text,
            references=[Reference(kind="test", title="source", snippet=payload.text)],
        )


class ModeratorSkill(EchoSkill):
    skill_id = "test.moderator"
    permissions = frozenset({SkillPermission.moderator})


class SlowSkill(EchoSkill):
    skill_id = "test.slow"
    limits = SkillLimits(timeout_s=0.01, max_output_chars=1_000)

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(10)
        finally:
            self.cancelled.set()
        return EchoOutput(text=payload.text)


class FailingSkill(EchoSkill):
    skill_id = "test.failing"

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        raise RuntimeError("SECRET community content")


class UnavailableSkill(EchoSkill):
    skill_id = "test.unavailable"

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        raise ProviderUnavailableError(detail="SECRET provider detail")


class ProviderErrorSkill(EchoSkill):
    skill_id = "test.provider-error"

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        raise ProviderError(detail="SECRET provider detail")


class ResearchProviderErrorSkill(EchoSkill):
    skill_id = "test.research-provider-error"

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        raise ResearchProviderError(detail="SECRET provider detail")


class ResearchNoResultsSkill(EchoSkill):
    skill_id = "test.research-no-results"

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        raise ResearchNoResultsError(detail="SECRET query")


class DictOutputSkill(EchoSkill):
    skill_id = "test.dict-output"

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        return {"text": payload.text}  # type: ignore[return-value]


class LargeOutputSkill(EchoSkill):
    skill_id = "test.large-output"
    limits = SkillLimits(timeout_s=1.0, max_output_chars=50)

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        self.calls += 1
        return EchoOutput(text="x" * 100)


def context(
    *,
    guild_id: int = 500,
    channel_id: int = 2002,
    parent_id: int | None = None,
    permissions: tuple[SkillPermission, ...] = (SkillPermission.guild_member,),
) -> SkillContext:
    return SkillContext(
        guild_id=guild_id,
        channel_id=channel_id,
        parent_id=parent_id,
        user_id=7,
        permissions=frozenset(permissions),
    )


def orchestrator(*skills, allowed_channel_ids=()) -> SkillOrchestrator:
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill)
    return SkillOrchestrator(
        registry,
        allowed_guild_ids=[500],
        allowed_channel_ids=allowed_channel_ids,
    )


async def test_valid_execution_returns_typed_output_and_references():
    skill = EchoSkill()
    result = await orchestrator(skill).run(TaskRequest(skill_id="test.echo", input={"text": "hello"}), context())

    assert result.status is SkillStatus.ok
    assert result.output == {
        "text": "hello",
        "references": [{"kind": "test", "title": "source", "url": None, "snippet": "hello"}],
    }
    assert result.references[0].title == "source"
    assert result.error is None
    assert skill.calls == 1


async def test_unknown_skill_is_rejected_without_execution():
    skill = EchoSkill()
    result = await orchestrator(skill).run(TaskRequest(skill_id="test.missing", input={"text": "hello"}), context())

    assert result.status is SkillStatus.unknown_skill
    assert result.output is None
    assert result.error == "Unknown skill."
    assert skill.calls == 0


async def test_invalid_input_is_rejected_without_execution():
    skill = EchoSkill()
    result = await orchestrator(skill).run(TaskRequest(skill_id="test.echo", input={"text": ""}), context())

    assert result.status is SkillStatus.invalid_input
    assert result.error == "Input does not match the skill's schema."
    assert skill.calls == 0


async def test_cross_guild_execution_is_denied_before_skill_runs():
    skill = EchoSkill()
    result = await orchestrator(skill).run(
        TaskRequest(skill_id="test.echo", input={"text": "hello"}),
        context(guild_id=999),
    )

    assert result.status is SkillStatus.denied
    assert result.output is None
    assert skill.calls == 0


async def test_channel_allowlist_accepts_parent_but_denies_other_channels():
    skill = EchoSkill()
    orch = orchestrator(skill, allowed_channel_ids=[42])

    allowed = await orch.run(
        TaskRequest(skill_id="test.echo", input={"text": "hello"}),
        context(channel_id=2002, parent_id=42),
    )
    denied = await orch.run(
        TaskRequest(skill_id="test.echo", input={"text": "hello"}),
        context(channel_id=2002, parent_id=99),
    )

    assert allowed.status is SkillStatus.ok
    assert denied.status is SkillStatus.denied
    assert skill.calls == 1


async def test_declared_permissions_are_enforced():
    skill = ModeratorSkill()
    orch = orchestrator(skill)

    denied = await orch.run(TaskRequest(skill_id="test.moderator", input={"text": "hello"}), context())
    allowed = await orch.run(
        TaskRequest(skill_id="test.moderator", input={"text": "hello"}),
        context(permissions=(SkillPermission.moderator,)),
    )

    assert denied.status is SkillStatus.denied
    assert allowed.status is SkillStatus.ok
    assert skill.calls == 1


async def test_timeout_is_safe_and_bounded():
    skill = SlowSkill()
    result = await orchestrator(skill).run(TaskRequest(skill_id="test.slow", input={"text": "hello"}), context())

    assert result.status is SkillStatus.timeout
    assert result.error == "The skill did not finish in time."
    assert skill.started.is_set()
    await asyncio.sleep(0)  # let wait_for finish cancelling the inner task
    assert skill.cancelled.is_set()


async def test_provider_unavailable_is_mapped_without_detail():
    skill = UnavailableSkill()
    result = await orchestrator(skill).run(TaskRequest(skill_id="test.unavailable", input={"text": "hello"}), context())

    assert result.status is SkillStatus.provider_unavailable
    assert "SECRET" not in (result.error or "")
    assert "Ollama" in (result.error or "")


async def test_provider_error_is_mapped_without_detail():
    skill = ProviderErrorSkill()
    result = await orchestrator(skill).run(
        TaskRequest(skill_id="test.provider-error", input={"text": "hello"}), context()
    )

    assert result.status is SkillStatus.provider_error
    assert "SECRET" not in (result.error or "")


@pytest.mark.parametrize(
    "skill, expected",
    [
        (ResearchProviderErrorSkill(), "provider returned an error"),
        (ResearchNoResultsSkill(), "no usable results"),
    ],
)
async def test_research_errors_keep_a_specific_provider_status(skill, expected):
    result = await orchestrator(skill).run(
        TaskRequest(skill_id=skill.skill_id, input={"text": "hello"}), context()
    )

    assert result.status is SkillStatus.provider_error
    assert expected in (result.error or "")
    assert "SECRET" not in (result.error or "")


async def test_skill_exception_is_safe_without_exception_text():
    skill = FailingSkill()
    result = await orchestrator(skill).run(TaskRequest(skill_id="test.failing", input={"text": "hello"}), context())

    assert result.status is SkillStatus.failed
    assert result.error == "The skill failed safely."
    assert "SECRET" not in (result.error or "")
    assert skill.calls == 1


@pytest.mark.parametrize(
    "skill",
    [DictOutputSkill(), LargeOutputSkill()],
)
async def test_invalid_or_oversize_output_is_rejected(skill):
    result = await orchestrator(skill).run(TaskRequest(skill_id=skill.skill_id, input={"text": "hello"}), context())

    assert result.status is SkillStatus.invalid_output
    assert result.output is None
    assert result.error in {
        "The skill returned an invalid result.",
        "The skill returned a result that was too large.",
    }


async def test_external_cancellation_propagates_and_cancels_the_skill():
    skill = SlowSkill()
    orch = orchestrator(skill)
    task = asyncio.create_task(orch.run(TaskRequest(skill_id="test.slow", input={"text": "hello"}), context()))
    await skill.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert skill.cancelled.is_set()
