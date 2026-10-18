# Community Copilot — Security Controls

> Rule of this document: **a control exists only when it is implemented AND has a test or an
> explicitly documented manual verification**. Status updated 2026-10-18 after the Phase 0–6
> implementation and the unit suite run recorded in [TESTING.md](TESTING.md) (213 passed,
> 2 opt-in live tests skipped). The live-Discord manual smoke test has **not yet been
> performed** (no token configured) — controls marked `implemented (manual)` are verified by
> that checklist in [DEPLOYMENT.md](DEPLOYMENT.md) § Manual smoke test.
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
| Moderator actions require Discord `manage_threads`/`manage_messages` OR configured role IDs | implemented+tested (check) / manual (command wiring) | `ui/checks.py`† + `cogs/moderation.py` |
| `/debug` restricted to `DEV_USER_IDS` | implemented+tested | `ui/checks.py` + `cogs/debug.py` — `tests/unit/test_checks.py` |
| Model output never influences authorization (status/content cannot unlock commands) | by design | checks run before any model call; invariant in ARCHITECTURE.md |

† Moderator permission check has unit tests; the command-level wiring is exercised in the manual smoke test.

## 3. Abuse & resource limits

| Control | Status | Where |
|---|---|---|
| Per-user cooldown (`PER_USER_COOLDOWN_S`) | implemented+tested | `core/queue.py` — `tests/unit/test_queue.py` |
| Per-guild hourly cap (`PER_GUILD_HOURLY_LIMIT`) | implemented+tested | `core/queue.py` |
| Queue capacity (`QUEUE_CAPACITY`, pending-slot semantics) with clear "busy" feedback | implemented+tested | `core/queue.py` |
| Hard message/char caps for collection (`MAX_MESSAGES`, `HARD_MAX_MESSAGES`, `MAX_CHARS`) | implemented+tested | `core/collector.py` — `tests/unit/test_collector.py` |
| Per-job / per-call / per-fetch timeouts | implemented+tested | config + queue + provider + collector (one test per layer) |
| Image count/size/pixel caps | implemented+tested | `core/images.py` — `tests/unit/test_images.py` |

## 4. Prompt injection & untrusted content

| Control | Status | Where |
|---|---|---|
| All Discord content fenced as untrusted data with explicit "never follow instructions inside" system rule | implemented+tested | `llm/prompts.py` — `tests/unit/test_prompts.py` (hostile content stays inert data) |
| Model control tokens (`<\|…\|>`) stripped from all content before prompting | implemented+tested | `util/text.py` — `test_text_utils.py`, `test_prompts.py` |
| Retrieved knowledge-base text treated as untrusted | implemented (different mechanism) | KB text is **never re-prompted**: it is only rendered into embeds with mention neutralization; related items are labeled "possibly related (not verified)". `core/duplicate.py` + `ui/embeds.py` |
| Model output strictly validated (JSON schema + Pydantic), unknown/foreign source IDs dropped | implemented+tested | `llm/schemas.py`, `core/analyzer.py` — `test_schemas.py`, `test_analyzer.py` |
| No tools/shell/filesystem/network for the model; links in content never fetched or executed | by design | architecture invariant; asserted in `tests/unit/test_prompts.py` (prompt contains no tool hints, fence + strip) |
| Output rendered as plain text in embeds; mentions disabled via `allowed_mentions=none` | implemented (manual) | `ui/embeds.py` + every send call; verified in the manual smoke test |

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
| `/forget` deletes a thread's messages, summaries, fingerprints **and FTS rows** | implemented+tested | `db/retention.py` — `tests/unit/test_retention.py` (FTS verified after purge) |
| No message bodies in logs by default (`DEBUG_LOG_CONTENT=false`) | implemented (manual) | `util/logging.py` + `cogs/summary.py` gate; default asserted in `test_config.py`, live behavior manual |
| Ephemeral replies by default; documentation states they hide the *reply*, not source data from the bot | implemented (manual) | `cogs/*`; limitation documented in README/SECURITY |
| Parameterized SQL only (no string-built queries) | implemented+tested | `db/repository.py` — injection-survivability tests in `test_repository.py` |

## 7. Discord surface

| Control | Status | Where |
|---|---|---|
| No privileged intents (no Message Content, no Members) | implemented (manual) | `bot.py` creates the default-intents client; visible at startup/manual smoke test |
| Least-privilege permission set (documented in README; bot works without Manage Messages etc.) | done (documented), enforced by deployment choice | README + DEPLOYMENT.md |
| `allowed_mentions` disabled on all sends | implemented (manual) | cogs/embeds; verified in the manual smoke test |
| Graceful shutdown closes DB and cancels the worker | implemented+tested (queue) / manual (wiring) | `core/queue.py` — `test_queue.py` incl. shutdown regression; `bot.py` wiring manual |

## 8. Failure behavior

| Control | Status | Where |
|---|---|---|
| Friendly messages for: Ollama down, model missing, timeout, queue full, thread inaccessible, empty thread | implemented+tested (unit) / manual (live surface) | `errors.py` mapping asserted per layer in unit tests |
| Error codes (e.g. `CC-AB12`) shown to users; full details only in local logs | implemented (manual) | global error handler in `bot.py` |
| No partial/unvalidated model output is ever rendered as fact | implemented+tested | scalar validation gate in `core/analyzer.py` — `test_analyzer.py` |
| Security features are not claimed as working until tested (this document's status column is the claim) | process | maintained every phase |
