# Community Copilot — Security Controls

> Rule of this document: **a control exists only when it is implemented AND has a test or an
> explicitly documented manual verification**. Status updated 2026-10-22 after the Phase 0.5
> tenancy/target-scope hardening (schema v4), the Phase 1 read-only agent/skill foundation, the
> Phase 2A backend approval state machine (schema v5), and the Phase 2B Content Studio
> (schema v6): explicit target threads are guild/allowlist checked, knowledge-base and insight
> queries are guild-scoped, only `approved=1` documents enter chat grounding or content drafts,
> skills are explicit typed application code rather than model tools, and content publication
> requires a stored human approval bound to the exact target/version plus a fresh authority and
> permission check at send time. Earlier live smoke evidence (mention chat, slash commands)
> remains in `data/logs/copilot.log`; see [PLAN.md](PLAN.md).
> Legend: `planned` · `implemented+tested` · `implemented (manual)` · `by design`.

## 1. Secrets

| Control | Status | Where |
|---|---|---|
| Bot token read only from environment (`.env`), never from code/args | implemented+tested | `config.py` — `SecretStr`; `tests/unit/test_config.py` (missing/empty token refused, no secret echo) |
| Redacting log formatter (registered secrets + token-shaped strings scrubbed) | implemented+tested | `util/logging.py` — `tests/unit/test_redaction.py` |
| Errors shown to users never contain secrets; full tracebacks only in local logs | implemented+tested | `errors.py` keeps `.detail` for logs only; user messages asserted detail-free in queue/checks/collector tests |
| `.env` gitignored before first commit; `.env.example` placeholders only | done | `.gitignore`, `.env.example` |
| No token in documentation, tests, fixtures, or terminal output | done (process) | tests use an obvious placeholder only; reviewed at each phase |

## 2. Authorization

| Control | Status | Where |
|---|---|---|
| Guild allowlist (`GUILD_ALLOWLIST`); empty list = bot refuses all guilds | implemented+tested | `ui/checks.py` — `tests/unit/test_checks.py` |
| Channel/thread allowlist (`CHANNEL_ALLOWLIST`; empty = all in allowed guilds) | implemented+tested | `ui/checks.py` — `tests/unit/test_checks.py` |
| Explicit target thread must belong to the interaction guild **and** pass the channel/parent allowlist before any read/write; missing/deleted/inaccessible targets fail closed | implemented+tested | `ui/checks.py::ensure_target_thread` — `test_checks.py`, `test_cogs_summary.py`, `test_cogs_moderation_correct_status.py`, `test_cogs_moderation_resolve.py`, `test_cogs_knowledge.py` |
| Solution approve/reject only sees a solution whose thread belongs to the interaction guild; cross-guild IDs are reported as not found | implemented+tested | `db/repository.py::get_solution_for_guild`, `core/solutions.py` — `test_solutions.py`, `test_cogs_moderation_approve.py`, `test_cogs_moderation_reject.py` |
| Moderator actions require Discord `manage_threads`/`manage_messages` OR configured role IDs | implemented+tested (check) / manual (command wiring) | `ui/checks.py`† + `cogs/moderation.py` |
| `/debug` restricted to `DEV_USER_IDS` | implemented+tested | `ui/checks.py` + `cogs/debug.py` — `tests/unit/test_checks.py` |
| Model output never influences authorization (status/content cannot unlock commands) | by design | checks run before any model call; invariant in ARCHITECTURE.md |
| Phase 1 skill dispatch requires the caller's guild/channel to pass the existing allowlists **and** the skill's declared permission set; unknown skills and invalid inputs fail before execution | implemented+tested | `agents/orchestrator.py`, `agents/registry.py` — `test_agents_orchestrator.py` (cross-guild, channel/parent, missing moderator, unknown ID, invalid input), `test_agents_contract.py` |
| Phase 1 registry accepts only typed `Skill` subclasses; duplicate IDs, arbitrary callables, invalid metadata, and non-read-only skills are rejected | implemented+tested | `agents/registry.py`, `agents/contract.py` — `test_agents_contract.py`; default registry contains only `kb.search` + `insights.community` (`test_agents_builtin.py`) |
| Phase 2A approval decisions require a trusted **human** actor with the `decide` permission, the request's guild, and a different actor ID from the requester; AI agents cannot approve their own proposed action | implemented+tested | `core/approvals.py::ApprovalService._decide` — `test_approvals.py` (agent/unpermitted/wrong-guild/self-approval denials) |
| Phase 2A execution claims require the `execute` permission and exact guild/target/action/content-version/SHA-256 binding; missing, rejected, expired, cancelled, mismatched, already-claimed or already-finished requests fail closed | implemented+tested | `core/approvals.py::begin_execution`, `db/repository.py::claim_approval_execution` — `test_approvals.py` (binding/expiry/state/idempotency cases) |
| Phase 2A transitions are conditional (`pending` → decided, `approved` → executing) and execution is unique per request/idempotency key, so concurrent decisions/claims cannot overwrite each other or duplicate a side effect; the same conditional pattern now protects existing solution approve/reject decisions | implemented+tested | `db/repository.py`, `core/solutions.py` — `test_approvals.py` (concurrent approve/reject, duplicate claims), `test_solutions.py` (concurrent decisions) |
| Phase 2A registers no generic public-action executor, approval command, or model tool; Phase 2B is the single content executor behind the same service | implemented+tested | `bot.py`, `core/approvals.py`, `core/publishing.py` - `test_bot.py`, `test_content_studio.py`; `PLAN.md` D25/D26 |
| Phase 2B Content Studio commands and approval buttons rebuild the actor from Discord on every interaction; no draft, target, approval or actor id from model output is trusted | implemented+tested | `cogs/content.py::_gate`/`perform_action`, `ui/checks.py` - `test_content_studio.py` (button callback auth, cross-guild isolation) |
| Phase 3 knowledge candidates validate the source thread/summary guild before storing anything, remain unapproved with no KB row, and can only be approved/rejected by a trusted human moderator with `decide` through the existing conditional solution transaction; agent actors, missing permissions and wrong-guild ids are denied | implemented+tested | `core/knowledge_candidates.py`, `cogs/knowledge.py`, `db/repository.py::approve_solution_with_kb` - `test_knowledge_candidates.py` (source/cross-guild/unauthorized/concurrent/stale cases) |
| Phase 3 Community Pulse reads only guild-scoped stored summaries and approved/unapproved KB rows; it emits no member profiles, yields no output below `INSIGHTS_MIN_THREADS`, and labels every recommendation as an estimate with an uncertainty indicator | implemented+tested | `core/community_pulse.py`, `agents/builtin.py::PulseSkill`, `cogs/pulse.py` - `test_community_pulse.py`, `test_cogs_pulse.py` |
| Content approval requires a different trusted human moderator than the author of the exact version; the current version author is the Phase 2A requester even when another moderator submits, and the editor of a new version cannot approve it | implemented+tested | `core/content_studio.py`, `core/approvals.py` - `test_content_studio.py` (self/cross-editor approval denial) |
| Publication rechecks the current Discord member's moderator rights, guild/channel allowlists, target existence and bot send permission immediately before claiming/sending; publishing is off by default | implemented+tested | `core/publishing.py::_revalidate_actor`, `ui/content.py::verify_actor`/`send_content`, `config.py` - `test_content_studio.py` (demotion, allowlist/target, disabled gate); live Discord not run |

* Moderator permission check has unit tests; the command-level wiring is exercised in the manual smoke test.

## 3. Abuse & resource limits

| Control | Status | Where |
|---|---|---|
| Per-user cooldown (`PER_USER_COOLDOWN_S`) | implemented+tested | `core/queue.py` — `tests/unit/test_queue.py` |
| Per-guild hourly cap (`PER_GUILD_HOURLY_LIMIT`) | implemented+tested | `core/queue.py` |
| Queue capacity (`QUEUE_CAPACITY`, pending-slot semantics) with clear "busy" feedback | implemented+tested | `core/queue.py` |
| Hard message/char caps for collection (`MAX_MESSAGES`, `HARD_MAX_MESSAGES`, `MAX_CHARS`) | implemented+tested | `core/collector.py` — `tests/unit/test_collector.py` |
| Per-job / per-call / per-fetch timeouts | implemented+tested | config + queue + provider + collector (one test per layer) |
| Phase 1 skill time/output bounds: each skill declares a timeout (registry cap 30 s) and serialized output cap (registry cap 20,000 chars); the orchestrator enforces both and propagates external cancellation | implemented+tested | `agents/contract.py`, `agents/orchestrator.py` — `test_agents_orchestrator.py` (timeout, oversize output, cancellation) |
| Image count/size/pixel caps | implemented+tested | `core/images.py` — `tests/unit/test_images.py` |
| Bounded simultaneous model calls (`OLLAMA_MAX_CONCURRENT`) and bounded chat waiters (`CONVERSATION_MAX_WAITING`) with one rate-limited notice beyond the bound | implemented+tested | `llm/ollama_provider.py`, `cogs/conversation.py` — `test_provider.py` (cap enforced; slot released on error/cancel), `test_cogs_conversation.py` (waiter cap; waiter count released on cancel) |
| Chat throttling feedback is non-escalating: at most one static notice per user per 5 minutes, even under a burst | implemented+tested | `cogs/conversation.py` — `test_cogs_conversation.py` |

## 4. Prompt injection & untrusted content

| Control | Status | Where |
|---|---|---|
| All Discord content fenced as untrusted data with explicit "never follow instructions inside" system rule | implemented+tested | `llm/prompts.py` — `tests/unit/test_prompts.py` (hostile content stays inert data) |
| Model control tokens (`<\|…\|>`) stripped from all content before prompting | implemented+tested | `util/text.py` — `test_text_utils.py`, `test_prompts.py` |
| Retrieved knowledge-base text treated as untrusted | implemented+tested (display paths and chat grounding — D22; live smoke step 19 pending) | Display paths never re-prompt KB text: it is rendered into embeds with mention neutralization, and related items are labeled "possibly related (not verified)" (`core/duplicate.py` + `ui/embeds.py`). Chat grounding admits **only guild-scoped `kb_docs` with `approved=1`** (auto-indexed thread fingerprints are excluded; defensive approved/kind checks), sanitized + fenced as untrusted data, top-k/char bounded, at most two source links labeled "possibly related (not verified)" and only for documents actually in the prompt, fail-open on retrieval errors; fence-integrity cases are unit-tested — hardening, not absolute injection prevention (`cogs/conversation.py`, `core/conversation.py`, `llm/persona.py` — `test_cogs_conversation.py`, `test_conversation.py`, `test_persona.py`) |
| Model output strictly validated (JSON schema + Pydantic), unknown/foreign source IDs dropped | implemented+tested | `llm/schemas.py`, `core/analyzer.py` — `test_schemas.py`, `test_analyzer.py` |
| No tools/shell/filesystem/network for the model; links in content never fetched or executed | by design | architecture invariant; asserted in `tests/unit/test_prompts.py` (prompt contains no tool hints, fence + strip) |
| Phase 1 skills are application adapters, not model tools: the model receives no skill names/schemas and cannot choose or invoke one; outputs are bounded records, never fed back as a prompt in this phase | by design + registry test | `agents/registry.py`, `agents/orchestrator.py` — `test_agents_contract.py`, `test_agents_builtin.py` (only read-only adapters registered) |
| Output rendered as plain text in embeds; mentions disabled via `allowed_mentions=none` | implemented (manual) | `ui/embeds.py` + every send call; verified in the manual smoke test |

| Phase 2B draft generation admits only guild-scoped approved `kb_docs` as optional context, sanitized + fenced with a case-insensitive fence-close scrub; the prompt forbids invented facts/citations/links/dates/commitments and requires `needs_research` when current facts are missing | implemented+tested | `llm/prompts.py::build_content_prompt`, `core/content_studio.py::_context` - `test_content_studio.py` (untrusted context, fence breakout, malformed/unavailable provider, research blocking) |
| Phase 2B generated text is validated (required/length/control characters/mass mentions) and `needs_research` is a blocking finding; exact-duplicate hash match is surfaced as a warning to the human reviewer | implemented+tested | `core/content_studio.py::_validate_content` - `test_content_studio.py` |

## 5. Attachments

| Control | Status | Where |
|---|---|---|
| Image formats allowed by **magic bytes**, not extension (PNG/JPEG/WebP) | implemented+tested | `core/images.py` — `tests/unit/test_images.py` |
| Count + byte-size caps (before and after download) | implemented+tested | same — one test per skip reason |
| Decompression-bomb guard (`Image.MAX_IMAGE_PIXELS`, explicit pixel cap) | implemented+tested | same |
| Re-encode through Pillow, strip metadata, bound dimensions | implemented+tested | same — verified by planting a `tEXt` metadata chunk and asserting it is absent in the sanitized copy |
| Skipped images reported, never silently dropped or falsely claimed as analyzed | implemented+tested | collector/analyzer skip notes — `test_images.py`, `test_analyzer.py` |

## 6. Data & privacy

| Control | Status | Where |
|---|---|---|
| Only summarized threads persisted; bounded excerpts | implemented+tested | `core/collector.py` caps + `db/repository.py` inserts |
| Retention window (`RETENTION_DAYS`) with periodic sweep | implemented+tested | `db/retention.py` — `tests/unit/test_retention.py` |
| Knowledge-base documents are guild-scoped; `/kb-search`, related-item lookup, and chat grounding cannot return another guild's rows | implemented+tested | `db/repository.py`, `core/duplicate.py`, `cogs/conversation.py` — `test_repository.py`, `test_duplicate.py`, `test_cogs_conversation.py` |
| Auto-indexed thread fingerprints are `approved=0` and excluded from approved-only retrieval; only approved solutions are chat grounding knowledge | implemented+tested | `db/schema.sql` (migration 4), `cogs/summary.py`, `core/solutions.py`, `core/conversation.py` — `test_migrations.py`, `test_cogs_summary.py`, `test_solutions.py`, `test_conversation.py` |
| `/insights` aggregates are guild-scoped | implemented+tested | `db/repository.py::summaries_since`, `core/insights_engine.py`, `cogs/insights.py` — `test_insights.py`, `test_repository.py` |
| Phase 1 `kb.search` adapter is guild-scoped and calls `search_kb(..., approved_only=True)`; `insights.community` uses the guild-scoped insights engine | implemented+tested | `agents/builtin.py` — `test_agents_builtin.py` (cross-guild rows excluded, unapproved fingerprint excluded, moderator gate) |
| `/forget` deletes a thread's messages, summaries, fingerprints **and FTS rows**, only after a same-guild target check; the purge is guild-scoped again in the database layer | implemented+tested | `db/retention.py`, `cogs/knowledge.py` — `tests/unit/test_retention.py`, `test_cogs_knowledge.py` (FTS verified after purge) |
| No message bodies in logs by default (`DEBUG_LOG_CONTENT=false`) | implemented (manual) | `util/logging.py` + `cogs/summary.py` gate; default asserted in `test_config.py`, live behavior manual |
| Ephemeral replies by default; documentation states they hide the *reply*, not source data from the bot | implemented (manual) | `cogs/*`; limitation documented in README/SECURITY |
| Parameterized SQL only (no string-built queries) | implemented+tested | `db/repository.py` — injection-survivability tests in `test_repository.py` |
| Chat turns stored scoped per (guild, channel, user); reads database-first and bounded (turn/char budget) | implemented+tested | `core/chat_memory.py`, `db/repository.py` — `test_chat_memory.py`, `test_repository.py` |
| Chat retention: age sweep (`CONVERSATION_RETENTION_DAYS`) in the daily loop + per-scope row cap (`CONVERSATION_MAX_STORED_TURNS`) pruned on every write | implemented+tested | `db/retention.py`, `db/repository.py` — `test_retention.py`, `test_repository.py` |
| `/clear-conversation` deletes only the caller's own turns in one channel/thread (in-flight-safe, transactional, audited count) | implemented+tested | `cogs/conversation.py` — `test_cogs_conversation.py` |
| The model can never widen scope or choose rows: prompts only ever contain the caller's own scope's turns (application code, not model-decided) | implemented+tested | `core/conversation.build_chat_messages` — `test_chat_memory.py` isolation tests |
| Database outage degrades chat to in-process context only (bounded); a failed write is never reported as persisted | implemented+tested | `core/chat_memory.py` — `test_chat_memory.py` |
| Clearing history does not delete summaries, corrections, solutions, KB documents, or audit rows | implemented+tested | `cogs/conversation.py` — `test_cogs_conversation.py` (unrelated-records regression) |
| Phase 2A approval rows store only structural action metadata and a SHA-256 content hash - never action content; structured audit rows record request, decision, expiry/cancellation, execution start and outcome; expiry changes state without deleting audit evidence, and existing thread/conversation retention does not purge approval records | implemented+tested | `db/schema.sql` (migration 5), `db/repository.py`, `core/approvals.py` - `test_approvals.py` (content absent from rows/audit; audit sequence; purge leaves approval evidence) |
| Phase 2B draft text is stored locally for review/version history (guild-scoped) but is never copied into approval rows or audit metadata; versions are immutable and edits append a new version; draft/version/approval/audit rows survive existing retention sweeps | implemented+tested | `db/schema.sql` (migration 6), `core/content_studio.py` - `test_content_studio.py` (immutable versions, retention), `test_migrations.py` |
| Phase 3 candidate audit metadata stores ids, source kind/summary, content hash and finding codes only - never candidate text. Automatic thread expiry removes pending candidates and unreviewed fingerprints but preserves approved `kb_docs`; only explicit `/forget` or `/knowledge-purge` deletes approved knowledge; audit rows are never swept | implemented+tested | `core/knowledge_candidates.py`, `db/retention.py`, `db/schema.sql` (migration 7) - `test_knowledge_candidates.py` (audit text absence, retention), `test_retention.py` |
| `/knowledge-purge` requires the allowlist, a fresh moderator check, a trusted human `decide` actor, and the document's guild; it deletes one approved `kb_docs` row and audits the event in one transaction. The audit stores ids/kind/source ref/body length, never title, URL or body; FTS follows via triggers; a failed audit rolls the delete back | implemented+tested | `cogs/knowledge.py`, `core/knowledge_purge.py`, `db/repository.py::purge_approved_kb_doc` - `test_knowledge_purge.py`, `test_cogs_knowledge.py` |
| Audit rows are append-only and retained indefinitely; no retention sweep, `/forget`, conversation clear or KB purge deletes them. Metadata is ids/counts/states/hashes/codes only; free-text notes are not copied into audit records | implemented+tested + documented | `db/repository.py`, `core/solutions.py`, `AUDIT.md` - `test_knowledge_purge.py` (metadata excludes content), `test_solutions.py` |

## 7. Discord surface

| Control | Status | Where |
|---|---|---|
| No privileged intents (Message Content, Members, Presence stay off) | implemented (manual) | `bot.py` enables only `guilds` + non-privileged guild message events (mention chat); visible in code/startup logs |
| Least-privilege permission set (documented in README; bot works without Manage Messages etc.) | done (documented), enforced by deployment choice | README + DEPLOYMENT.md |
| `allowed_mentions` disabled on all sends | implemented (manual) | cogs/embeds; verified in the manual smoke test |
| Graceful shutdown closes DB and cancels the worker | implemented+tested (queue) / manual (wiring) | `core/queue.py` — `test_queue.py` incl. shutdown regression; `bot.py` wiring manual |

## 8. Failure behavior

| Control | Status | Where |
|---|---|---|
| Friendly messages for: Ollama down, model missing, timeout, queue full, thread inaccessible, empty thread | implemented+tested (unit) / manual (live surface) | `errors.py` mapping asserted per layer in unit tests |
| Error codes (e.g. `CC-AB12`) shown to users; full details only in local logs | implemented (manual) | global error handler in `bot.py` |
| No partial/unvalidated model output is ever rendered as fact | implemented+tested | scalar validation gate in `core/analyzer.py` — `test_analyzer.py` |
| Phase 1 skill failures (unknown, invalid input, denied, timeout, provider unavailable/error, invalid output, unexpected exception) become structured results with safe messages; exception text is not copied into errors and logs carry the skill ID + exception type only | implemented+tested | `agents/orchestrator.py` — `test_agents_orchestrator.py` (`SECRET`-shaped exception/provider detail never appears) |
| Phase 2A approval/execution failures use typed safe messages (not row data, content or storage detail); request/decision/execution-start/outcome writes are transactional, and an audit failure rolls the state change back | implemented+tested | `errors.py`, `core/approvals.py`, `db/repository.py` - `test_approvals.py` (audit-trigger rollback, storage contention leaves request pending) |
| Phase 2B publishing is at-most-once: success is recorded only after Discord returns a message id; definite send failures are recorded as failed; timeout/5xx/unknown sends leave `executing` and are never retried automatically. `/content-recover` records success only when the exact nonce + content is found in the target channel, otherwise the claim is left untouched | implemented+tested | `core/publishing.py`, `ui/content.py`, `core/approvals.py::reconcile_execution` - `test_content_studio.py` (definite/ambiguous/timeout/rate-limit, restart recovery, no-evidence leaves claim) |
| Honest delivery: sending stops at the first failed chunk; failures are logged once (typed + redacted) and reported as `delivered=False` — never claimed as sent | implemented+tested | `cogs/conversation.py` — `test_cogs_conversation.py` (single-chunk, partial and rate-limit failures) |
| Chat timeouts and cancellations release the scope guard, the model slot and the typing indicator (`finally` on every path) and persist nothing | implemented+tested | `cogs/conversation.py`, `llm/ollama_provider.py` — `test_cogs_conversation.py`, `test_provider.py` |
| Security features are not claimed as working until tested (this document's status column is the claim) | process | maintained every phase |
