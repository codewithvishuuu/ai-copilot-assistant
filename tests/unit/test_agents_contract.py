"""Phase 1 skill contract: typed registration, duplicate rejection, no functions."""

from __future__ import annotations

import pytest
from pydantic import Field, ValidationError

from copilot.agents.contract import (
    Reference,
    Skill,
    SkillContext,
    SkillErrorPolicy,
    SkillInput,
    SkillLimits,
    SkillOutput,
    SkillPermission,
    TaskRequest,
)
from copilot.agents.registry import SkillRegistry


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

    async def execute(self, context: SkillContext, payload: EchoInput) -> EchoOutput:
        return EchoOutput(
            text=payload.text,
            references=[Reference(kind="test", title="test source", snippet=payload.text)],
        )


class BadIdSkill(EchoSkill):
    skill_id = "Bad/ID"


class NonReadOnlySkill(EchoSkill):
    skill_id = "test.write"
    read_only = False


class NoPermissionsSkill(EchoSkill):
    skill_id = "test.no-permissions"
    permissions = frozenset()


class BadLimitsSkill(EchoSkill):
    skill_id = "test.bad-limits"
    limits = SkillLimits(timeout_s=0.0, max_output_chars=100)


class BadPolicySkill(EchoSkill):
    skill_id = "test.bad-policy"
    error_policy = "raise"


def test_registry_registers_resolves_and_orders_typed_skills():
    registry = SkillRegistry()
    skill = EchoSkill()

    registry.register(skill)

    assert registry.ids == ("test.echo",)
    assert registry.get("test.echo") is skill
    assert registry.require("test.echo") is skill
    assert registry.all() == (skill,)
    assert len(registry) == 1


def test_registry_rejects_duplicate_ids():
    registry = SkillRegistry()
    registry.register(EchoSkill())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoSkill())


def test_registry_rejects_arbitrary_callables():
    registry = SkillRegistry()

    async def not_a_skill(context, payload):
        return payload

    with pytest.raises(TypeError, match="Only Skill subclasses"):
        registry.register(not_a_skill)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "skill, message",
    [
        (BadIdSkill(), "Skill IDs"),
        (NonReadOnlySkill(), "not read-only"),
        (NoPermissionsSkill(), "at least one permission"),
        (BadLimitsSkill(), "timeout"),
        (BadPolicySkill(), "safe_error"),
    ],
)
def test_registry_rejects_invalid_skill_metadata(skill, message):
    registry = SkillRegistry()

    with pytest.raises(ValueError, match=message):
        registry.register(skill)


def test_registry_rejects_wrong_typed_models():
    class WrongInputSkill(EchoSkill):
        skill_id = "test.wrong-input"
        input_model = object

    class WrongOutputSkill(EchoSkill):
        skill_id = "test.wrong-output"
        output_model = object

    with pytest.raises(ValueError, match="input_model"):
        SkillRegistry().register(WrongInputSkill())
    with pytest.raises(ValueError, match="output_model"):
        SkillRegistry().register(WrongOutputSkill())


def test_unknown_ids_are_absent_not_executable():
    registry = SkillRegistry()

    assert registry.get("test.missing") is None
    with pytest.raises(KeyError):
        registry.require("test.missing")


def test_request_and_input_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        TaskRequest(skill_id="test.echo", input={}, extra="nope")
    with pytest.raises(ValidationError):
        EchoInput(text="hello", extra="nope")
