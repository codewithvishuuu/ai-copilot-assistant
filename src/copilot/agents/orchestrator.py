"""Deterministic, bounded skill orchestrator.

Dispatch is explicit: the caller names a registered skill ID. The model never
chooses a skill, never receives tool definitions, and never sees an execution
path that can bypass guild/channel or permission checks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from ..errors import (
    ModelNotInstalledError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..ui.checks import channel_allowed, guild_allowed
from .contract import Skill, SkillContext, SkillResult, SkillStatus, TaskRequest
from .registry import SkillRegistry

log = logging.getLogger(__name__)


class SkillOrchestrator:
    """Resolve, authorize, execute, and validate one skill request."""

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        allowed_guild_ids: Sequence[int],
        allowed_channel_ids: Sequence[int],
    ) -> None:
        self.registry = registry
        self._allowed_guild_ids = tuple(allowed_guild_ids)
        self._allowed_channel_ids = tuple(allowed_channel_ids)

    async def run(self, request: TaskRequest, context: SkillContext) -> SkillResult:
        """Execute one request and always return a safe structured result.

        External cancellation is propagated (it is not converted into a result);
        all ordinary failures become bounded, safe ``SkillResult`` values.
        """
        if not self._scope_allowed(context):
            return self._denied(request.skill_id)

        skill = self.registry.get(request.skill_id)
        if skill is None:
            return self._failure(SkillStatus.unknown_skill, request.skill_id, "Unknown skill.")
        permissions = getattr(context, "permissions", None)
        if not isinstance(permissions, (set, frozenset)):
            return self._denied(request.skill_id)
        if not skill.permissions.issubset(permissions):
            return self._denied(request.skill_id)

        try:
            payload = skill.input_model.model_validate(request.input)
        except ValidationError:
            return self._failure(
                SkillStatus.invalid_input,
                request.skill_id,
                "Input does not match the skill's schema.",
            )

        try:
            output = await asyncio.wait_for(skill.execute(context, payload), timeout=skill.limits.timeout_s)
        except TimeoutError:
            log.info("Skill timed out (skill=%s)", skill.skill_id)
            return self._failure(
                SkillStatus.timeout,
                request.skill_id,
                "The skill did not finish in time.",
            )
        except asyncio.CancelledError:
            # Cancellation belongs to the caller/queue; never swallow it.
            raise
        except ProviderUnavailableError as exc:
            log.warning("Skill provider unavailable (skill=%s)", skill.skill_id)
            return self._failure(SkillStatus.provider_unavailable, request.skill_id, exc.user_message())
        except ModelNotInstalledError as exc:
            log.warning("Skill model unavailable (skill=%s)", skill.skill_id)
            return self._failure(SkillStatus.provider_unavailable, request.skill_id, exc.user_message())
        except ProviderTimeoutError as exc:
            log.info("Skill provider timed out (skill=%s)", skill.skill_id)
            return self._failure(SkillStatus.timeout, request.skill_id, exc.user_message())
        except ProviderError as exc:
            log.warning("Skill provider error (skill=%s)", skill.skill_id)
            return self._failure(SkillStatus.provider_error, request.skill_id, exc.user_message())
        except Exception as exc:  # noqa: BLE001 - skill failures must not crash the bot
            # Type only: exception text may contain community content or secrets.
            log.error("Skill failed (skill=%s type=%s)", skill.skill_id, type(exc).__name__)
            return self._failure(
                SkillStatus.failed,
                request.skill_id,
                "The skill failed safely.",
            )

        return self._validated_output(skill, request.skill_id, output)

    # ---------------------------------------------------------------- internals

    def _scope_allowed(self, context: SkillContext) -> bool:
        guild_id = getattr(context, "guild_id", None)
        channel_id = getattr(context, "channel_id", None)
        if not isinstance(guild_id, int) or guild_id <= 0:
            return False
        if not isinstance(channel_id, int) or channel_id <= 0:
            return False
        if not guild_allowed(self._allowed_guild_ids, guild_id):
            return False
        return channel_allowed(self._allowed_channel_ids, channel_id, getattr(context, "parent_id", None))

    def _validated_output(self, skill: Skill[Any, Any], skill_id: str, output: object) -> SkillResult:
        if not isinstance(output, skill.output_model):
            log.warning("Skill returned invalid output type (skill=%s)", skill.skill_id)
            return self._failure(
                SkillStatus.invalid_output,
                skill_id,
                "The skill returned an invalid result.",
            )

        try:
            serialized = output.model_dump(mode="json")
            encoded = json.dumps(serialized, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            log.warning("Skill output could not be serialized (skill=%s)", skill.skill_id)
            return self._failure(
                SkillStatus.invalid_output,
                skill_id,
                "The skill returned an invalid result.",
            )

        if len(encoded) > skill.limits.max_output_chars:
            log.warning("Skill output exceeded its bound (skill=%s)", skill.skill_id)
            return self._failure(
                SkillStatus.invalid_output,
                skill_id,
                "The skill returned a result that was too large.",
            )

        return SkillResult(
            status=SkillStatus.ok,
            skill_id=skill_id,
            output=serialized,
            references=list(output.references),
        )

    @staticmethod
    def _denied(skill_id: str) -> SkillResult:
        return SkillResult(
            status=SkillStatus.denied,
            skill_id=skill_id,
            error="This skill is not available for this request.",
        )

    @staticmethod
    def _failure(status: SkillStatus, skill_id: str, message: str) -> SkillResult:
        # Safe messages are normally short; truncation guarantees a custom
        # provider message can never make result construction fail.
        return SkillResult(status=status, skill_id=skill_id, error=message[:300])
