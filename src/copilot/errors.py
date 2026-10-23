"""Typed application errors with friendly user-facing messages.

Rules:
- Anything shown to a Discord user must come from here (never a raw traceback).
- Details (for local logs only) go into ``.detail``, never into user messages.
"""

from __future__ import annotations

import math
import secrets


def new_error_code() -> str:
    """Short correlation code shown to users and written to local logs."""
    return "CC-" + secrets.token_hex(2).upper()


class CopilotError(Exception):
    """Base class: message text is safe to show to end users."""

    default_message = "Something went wrong while processing the request."

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.detail = detail

    def user_message(self) -> str:
        return str(self)


# --------------------------------------------------------------------- startup


class ConfigError(Exception):
    """Raised during startup when configuration is missing or invalid."""


# -------------------------------------------------------------------- provider


class ProviderUnavailableError(CopilotError):
    default_message = (
        "The local AI server (Ollama) is not reachable. "
        "Start Ollama on the machine running this bot and try again."
    )


class ModelNotInstalledError(CopilotError):
    default_message = (
        "The configured AI model is not installed in Ollama. "
        "Ask the bot operator to run: ollama pull <model>"
    )


class ProviderTimeoutError(CopilotError):
    default_message = (
        "The local AI model took too long to respond. "
        "Try again, or ask the operator to check the machine's load."
    )


class ProviderError(CopilotError):
    default_message = "The local AI model returned an error. Please try again later."


# ---------------------------------------------------------------------- output


class OutputValidationError(CopilotError):
    default_message = (
        "The AI model did not produce a usable result for this request. "
        "Nothing was saved. You can try again."
    )


class EmptyModelOutputError(OutputValidationError):
    default_message = (
        "The AI model returned an empty response (a known small-model failure mode). "
        "Please try again."
    )


# ------------------------------------------------------------------ permission


class NotAllowedError(CopilotError):
    default_message = "Community Copilot is not enabled in this server or channel."


class NotModeratorError(CopilotError):
    default_message = "This command requires moderator permissions."


class NotDevUserError(CopilotError):
    default_message = "This command is restricted to the bot's developer."


# ------------------------------------------------------------------- approvals


class ApprovalNotFoundError(CopilotError):
    default_message = "No approval request with that id was found for this server."


class ApprovalDeniedError(CopilotError):
    default_message = "You are not allowed to act on this approval request."


class ApprovalSelfApprovalError(ApprovalDeniedError):
    default_message = "The requester cannot approve their own action."


class ApprovalConflictError(CopilotError):
    default_message = "This approval request is not in a state that allows that action."


class ApprovalExpiredError(ApprovalConflictError):
    default_message = "This approval request has expired."


class ApprovalBindingError(CopilotError):
    default_message = "The action does not match the approved request."


# ------------------------------------------------------------------- missions


class MissionNotFoundError(CopilotError):
    default_message = "No mission with that id was found for this server."


class MissionConflictError(CopilotError):
    default_message = "This mission is not in a state that allows that action."


class MissionPlanError(CopilotError):
    default_message = (
        "The mission could not be planned within the allowed limits. Try a narrower goal."
    )


# ---------------------------------------------------------------- host agent


class AgentRunNotFoundError(CopilotError):
    default_message = "No agent run with that id was found for this server."


class AgentRunConflictError(CopilotError):
    default_message = "This agent run is not in a state that allows that action."


class AgentPlanError(CopilotError):
    default_message = (
        "The request could not be planned within the allowed limits or no required "
        "capability is available. Try a narrower goal."
    )


# --------------------------------------------------------------- attachments


class AttachmentUnsupportedError(CopilotError):
    default_message = (
        "That attachment format is not supported. Supported formats are images, PDF, "
        "plain text, common source code, and log files."
    )


# ---------------------------------------------------------------- moderation


class ModerationPolicyError(CopilotError):
    default_message = "The moderation policy input is invalid. Check the values and try again."


class ModerationIncidentNotFoundError(CopilotError):
    default_message = "No moderation incident with that id was found for this server."


class ModerationIncidentConflictError(CopilotError):
    default_message = "This moderation incident was already reviewed. Nothing was changed."


# ---------------------------------------------------------------- scheduling


class ScheduledJobNotFoundError(CopilotError):
    default_message = "No scheduled job with that id was found for this server."


class ScheduledJobConflictError(CopilotError):
    default_message = "This scheduled job is not in a state that allows that action."


class ScheduleError(CopilotError):
    default_message = "The schedule input is invalid. Check the time and timezone and try again."


# ----------------------------------------------------------- reply approvals


class ReplyApprovalNotFoundError(CopilotError):
    default_message = "No reply approval with that id was found for this server."


class ReplyApprovalConflictError(CopilotError):
    default_message = "This reply approval is not in a state that allows that action."



# ------------------------------------------------------------------- research


class ResearchUnavailableError(CopilotError):
    default_message = (
        "Web research is not available on this bot right now. "
        "No sources were invented; ask the operator to configure a search provider."
    )


class ResearchProviderError(CopilotError):
    default_message = (
        "The web search provider returned an error. The research request stopped; "
        "nothing was saved as a verified source."
    )


class ResearchNoResultsError(CopilotError):
    default_message = "Web search returned no usable results for that query."


class ResearchReportNotFoundError(CopilotError):
    default_message = "No research report with that id was found for this server."


# ------------------------------------------------------------- content planning


class EventPlanNotFoundError(CopilotError):
    default_message = "No event plan with that id was found for this server."


class CalendarEntryNotFoundError(CopilotError):
    default_message = "No calendar entry with that id was found for this server."


class ContextualReplyNotFoundError(CopilotError):
    default_message = "No reply draft with that id was found for this server."


class ContentPlanError(CopilotError):
    default_message = "The calendar or event plan input is invalid. Check the details and try again."


class AgentEvalError(CopilotError):
    default_message = "The offline evaluation could not be completed. Nothing was changed."


# ---------------------------------------------------------------- content studio


class ContentDraftNotFoundError(CopilotError):
    default_message = "No content draft with that id was found for this server."


class ContentDraftConflictError(CopilotError):
    default_message = "This content draft is not in a state that allows that action."


class ContentValidationError(CopilotError):
    default_message = (
        "This content did not pass validation. Fix the findings shown in the draft before "
        "requesting approval."
    )


class ContentTargetError(CopilotError):
    default_message = (
        "The target channel is not available in this server, or I do not have permission "
        "to post there."
    )


class ContentPublishingDisabledError(CopilotError):
    default_message = (
        "Content publishing is disabled on this bot. Approval and review still work; "
        "ask the operator to enable CONTENT_PUBLISHING_ENABLED."
    )


class ContentPublishError(CopilotError):
    default_message = "Discord did not confirm the publication, so it was not marked as published."


# ------------------------------------------------------------ community pulse
class PulseRecommendationNotFoundError(CopilotError):
    default_message = (
        "That Community Pulse recommendation is no longer reproducible from the current "
        "guild data. Run /pulse again and pick a current recommendation."
    )


# ------------------------------------------------------ knowledge candidates
class KnowledgeCandidateNotFoundError(CopilotError):
    default_message = "No knowledge candidate with that id was found for this server."


class KnowledgeCandidateSourceError(CopilotError):
    default_message = (
        "The candidate source is missing, invalid, or belongs to a different server."
    )


class KnowledgeCandidateStaleError(CopilotError):
    default_message = (
        "This candidate was based on an older summary. Review the latest source before "
        "approving it."
    )


class KnowledgeCandidateConflictError(CopilotError):
    default_message = (
        "This knowledge candidate was already reviewed. Nothing was changed; refresh the "
        "candidate list to see its current state."
    )


class KnowledgeCandidateDuplicateError(KnowledgeCandidateConflictError):
    default_message = (
        "An identical knowledge candidate already exists in this server. Review that "
        "candidate instead of creating a duplicate."
    )


class KnowledgeDocumentNotFoundError(CopilotError):
    default_message = (
        "No approved knowledge document with that id was found for this server."
    )


class KnowledgeDocumentNotApprovedError(CopilotError):
    default_message = (
        "Only approved knowledge documents can be purged. Unreviewed content is handled "
        "by the thread retention or /forget paths."
    )


# ------------------------------------------------------------------- scheduling


class QueueFullError(CopilotError):
    default_message = "The bot is busy with other requests right now. Please try again in a minute."


class CooldownError(CopilotError):
    default_message = "Please wait a moment before using this command again."

    def __init__(self, retry_after_s: float, *, detail: str | None = None) -> None:
        wait = max(1, math.ceil(retry_after_s))
        super().__init__(
            f"Please wait about {wait} second{'s' if wait != 1 else ''} before trying again.",
            detail=detail,
        )
        self.retry_after_s = retry_after_s


class GuildLimitError(CopilotError):
    default_message = (
        "This server has reached its hourly request limit for Community Copilot. "
        "Please try again later."
    )

    def __init__(self, retry_after_s: float, *, detail: str | None = None) -> None:
        super().__init__(
            f"This server has reached its hourly limit. Try again in about "
            f"{max(1, math.ceil(retry_after_s / 60))} minute(s).",
            detail=detail,
        )
        self.retry_after_s = retry_after_s


class JobCancelledError(CopilotError):
    default_message = "Request cancelled."


class JobTimedOutError(ProviderTimeoutError):
    default_message = (
        "This request took too long and was stopped. "
        "Try again with a smaller thread or a smaller message count."
    )


# --------------------------------------------------------------------- discord


class ThreadInaccessibleError(CopilotError):
    default_message = (
        "I can't read that thread. Make sure it's a forum post or thread in this server "
        "and that I have permission to view its history."
    )


class EmptyThreadError(CopilotError):
    default_message = "That thread has no usable messages to summarize."


# -------------------------------------------------------------------- storage


class DatabaseError(CopilotError):
    default_message = "The local database had a problem. Nothing was saved; please try again."


class VisionNotSupportedError(CopilotError):
    default_message = "Image analysis is not available right now; the summary will use text only."
