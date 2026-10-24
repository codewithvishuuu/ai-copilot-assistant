# Community Copilot — Testing Guide

## Philosophy

- **Deterministic by default.** Unit tests never touch the network, real Discord, or a real model.
- **Fakes over mocks where behavior matters.** A small fake Ollama provider and fake Discord
  objects (`tests/unit/fakes.py`) stand in for the real things, so tests read like the real flow.
- **Live checks are opt-in** and marked `@pytest.mark.live_ollama` — skipped unless
  `RUN_LIVE_OLLAMA=1` is set and a local Ollama server with the configured model is reachable.
- **No fabricated results.** This document only lists runs that actually happened, with the
  exact command and outcome (see the recorded run at the bottom).

## Running

```powershell
.venv\Scripts\python.exe -m pytest tests/unit            # unit suite (mock-only, default)
.venv\Scripts\python.exe -m pytest                       # everything; live tests auto-skip
.venv\Scripts\python.exe -m pytest tests\unit\test_queue.py -q   # one file
.venv\Scripts\python.exe -m ruff check src tests scripts         # lint (must be clean)

# live model round-trip against the real local server (opt-in):
$env:RUN_LIVE_OLLAMA="1"
.venv\Scripts\python.exe -m pytest tests/integration -q
Remove-Item Env:\RUN_LIVE_OLLAMA
```

## Suite layout (what exists today)

| Path | Covers |
|---|---|
| `tests/unit/fakes.py` | FakeProvider (canned outputs, capabilities, health), FakeThread/FakeMessage/FakeAttachment (incl. download errors + delays), FakeInteraction, `fake_settings()` |
| `tests/unit/conftest.py` | `tmp_db`, `tmp_settings`, secret-registry cleanup |
| `tests/unit/test_config.py` | defaults, list parsing, `SecretStr` token, empty-allowlist denial, `validate_for_run` |
| `tests/unit/test_text_utils.py` | truncation, whitespace normalization, control-token stripping, mention neutralization, FTS query building |
| `tests/unit/test_redaction.py` | token-shaped strings + registered secrets scrubbed from logs and tracebacks |
| `tests/unit/test_checks.py` | guild/channel allowlists, explicit target-thread guild/allowlist checks, denial messages, moderator (permission + role), dev-user gate |
| `tests/unit/test_database.py` | migrations apply exactly once, WAL/FK pragmas, ping/close, transactional rollback |
| `tests/unit/test_queue.py` | capacity (pending-slot semantics), cooldown, guild cap, timeout, cancel pending + active, shutdown incl. the `wait_for` swallow regression |
| `tests/unit/test_repository.py` | parameterized CRUD, upserts, guild-scoped FTS5 search/approved-only filter, injection survivability, FTS trigger sync, audit JSON |
| `tests/unit/test_retention.py` | expiry math, full thread purge, guild-scoped purge refusal, idempotency, channel-scoped turns/incidents/warnings/replies/attachments removed while other channels and guilds survive, purge without a thread row, sweep leaves non-expired data |
| `tests/unit/test_solutions.py` | propose → approve (KB promotion + searchability) / reject, guild-scoped decisions, audit rows, not-found paths, concurrent decisions have one winner and at most one KB document |
| `tests/unit/test_duplicate.py` | guild-scoped BM25 related-doc mapping, source-thread exclusion, limit, fingerprint text |
| `tests/unit/test_insights.py` | minimum-data gate, guild-scoped aggregates, malformed JSON tolerance, signature cleaning/caps, window math |
| `tests/unit/test_schemas.py` | JSON extraction (braces/escapes/garbage), status/confidence normalization, bounded lists, debug schema, flat decoding schemas |
| `tests/unit/test_prompts.py` | thread/message fencing, hostile content kept as inert data, repair + debug prompt shapes |
| `tests/unit/test_provider.py` | request shape (`think`, format, options), schema→`json` fallback, timeouts, 404 mapping, redaction, capability cache, `strip_thinking`, shared generation semaphore (cap enforced, slot released on error/cancel, fallback cannot deadlock a single slot) |
| `tests/unit/test_collector.py` | opening/reply ordering, fetch fallback + note, skip accounting, dedup, caps, char budget, friendly errors |
| `tests/unit/test_images.py` | magic-byte sniffing, metadata stripping, downscale, pixel/byte caps, JPEG fallback, every skip reason |
| `tests/unit/test_analyzer.py` | happy path, invented-ID dropping, no-repair-on-empty-content, single repair retry (success/failure), vision gating |
| `tests/unit/test_embeds.py` | Discord limits, paging by fields and by text budget, mention neutralization, correction/related/notes sections, honest status wording |
| `tests/unit/test_health.py` | truthful per-subsystem status (down/missing/ok), capabilities not probed when unreachable, health never raises |
| `tests/unit/test_conversation.py` | chat trigger policy, bounded context store, `build_chat_messages`, fence-safe reply splitting (boundaries, limits, Hindi text, balanced fences) |
| `tests/unit/test_chat_memory.py` | durable scoped turns, DB-first reads, clear semantics, honest degradation on DB failure, scope-only memory stats (counts/timestamps, never content) |
| `tests/unit/test_cogs_conversation.py` | listener/dispatch wiring, send failures (single/partial/rate-limited) with truthful `delivered=`, cooldown boundaries via injected clock, waiter cap + rate-limited notices, cancellation/timeout cleanup, redelivery dedup, clear-while-in-flight, `/conversation-status` counts/no-content/cross-guild denial; approved-only, guild-scoped grounding |
| `tests/unit/test_cogs_summary.py` | `/summarize` target selection, cross-guild/off-allowlist refusal, queue flow, persistence, fingerprint indexing |
| `tests/unit/test_cogs_moderation_*.py` | correction/resolve/approve/reject target and guild-scope denials, ack ordering, state changes |
| `tests/unit/test_cogs_knowledge.py` | `/forget` same-guild purge including channel-scoped turns/attachments with audit retention, cross-guild/off-allowlist/non-moderator denials; `/knowledge-purge` success, non-moderator and cross-guild refusals |
| `tests/unit/test_cogs_automod.py` | mention-only scope (ordinary messages and non-bot mentions are not assessed), policy command uses `MODERATION_DEFAULT_CONFIDENCE` unless overridden |
| `tests/unit/test_knowledge_purge.py` | approved document lookup/purge: authorization, guild isolation, missing/unapproved ids, FTS cleanup, audit metadata without content, failed-audit rollback, concurrent one-winner, orphan cleanup |
| `tests/unit/test_persona.py` | persona prompt contract + versioning, static reply constants |
| `tests/unit/test_migrations.py` | in-place migration upgrade (v2 → v10, seeded data + FTS intact, `guild_id`/`approved` backfill, approval tables/indexes, Content Studio tables + `approval_executions.result_ref`, solution source/hash columns, mission/research/planning/evaluation tables, draft provenance, `audit_log.guild_id`, attachments/voice/moderation/agent/scheduler tables + `reply_approvals`); v10 agent-table rebuild preserves runs/steps and accepts the evaluation mode; approved-only default retrieval after upgrade; mid-block failure rolls back and closes the connection; fresh DB reaches v10 with full integrity |
| `tests/unit/test_community_pulse.py` | minimum-data thresholds, reproducible evidence-backed recommendations, repeated questions/errors, common topics, approved-KB gap suppression, guild isolation, draft-only conversion |
| `tests/unit/test_knowledge_candidates.py` | source/guild validation, exact duplicate refusal, possible-contradiction hints, unauthorized/cross-guild review, stale summaries, concurrent approve/reject, approved-vs-unapproved retrieval, retention |
| `tests/unit/test_cogs_pulse.py` | `/pulse` threshold honesty, evidence rendering and estimate labeling, moderator gate |
| `tests/unit/test_bot.py` | real `commands.Bot` wiring: intents, event dispatch end-to-end, redelivered message events, approval/content service wiring and content command registration |
| `tests/unit/test_approvals.py` | Phase 2A approval state machine: human-only decisions, self-approval/agent/wrong-guild denial, expiry/cancellation, exact target/content-version/hash binding, concurrent decisions, execution claim uniqueness/idempotency, terminal outcomes, transactional audit rollback, storage-contention fail-closed, retention leaves approval evidence |
| `tests/unit/test_content_studio.py` | Phase 2B Content Studio: bounded generation/validation, research blocking, malformed/unavailable provider, untrusted KB context/fence, immutable versions, cross-guild isolation, duplicate warning, exact-version approval, edit/target invalidation, missing/rejected/cancelled/expired approvals, callback authorization, human-only publish, disabled gate, idempotent/duplicate publication, definite/ambiguous/timeout/rate-limit adapter outcomes, restart recovery without evidence, permission changes, audit/retention; guild voice profile, feedback reuse, approved-history grounding and exact-hash/near-duplicate repetition checks |
| `tests/unit/test_agents_contract.py` | typed skill metadata, duplicate IDs, arbitrary-callable rejection, read-only enforcement, unknown lookup, extra-field rejection |
| `tests/unit/test_agents_orchestrator.py` | guild/channel/permission authorization before execution, unknown/invalid input, timeout, provider failure mapping, safe exceptions, output bounds, cancellation |
| `tests/unit/test_agents_builtin.py` | approved-only/guild-scoped `kb.search`, guild-scoped `insights.community`, evidence-backed `radar.community`, read-only `moderation.assess`, lexical `triage.analyze` with secret redaction, unpersisted `content.brief`, read-only `quality.checks`, moderator gates, read-only default registry, bot wiring, all eight roles backed by registered skills |
| `tests/unit/test_agents_host.py` | goal classification/planner bounds, registered-skill-only plans, dependency-ordered synthesis, every host mode orchestrating its specialist roles, honest unavailable web/content/moderation/quality roles, upstream output passing, bounded parallel barrier, partial failure → skipped dependents, cancellation, idempotency, disabled setting, cross-guild get/cancel denial, non-moderator denial, off-allowlist failure |
| `tests/unit/test_attachments.py` | magic-byte detection over filename/MIME, size/count/page/pixel caps, image re-encode, PDF/DOCX/text extraction bounds, unsupported-format messages, retention/deletion scope, prompt fencing and follow-up references |
| `tests/unit/test_moderation_ai.py` | guild-scoped policy evaluation, context vs isolated keywords, severity/confidence thresholds, policy-enabled deletion with permission, disabled policy, missing permission, ambiguous/low-confidence flags, repeat incidents, cross-guild denial, retry idempotency, privacy-safe audit, private-channel report delivery with fakes, no ban/timeout |
| `tests/unit/test_scheduler.py` | read-only job kinds only (no publish), guild-scoped idempotency, timezone parsing/rejection, due-job execution + audit, digest mode, draft provenance, retry→fail, timeout retry, cancel + restart recovery, off-allowlist permanent failure, calendar-link cancellation, cooperative cancellation of an in-flight task, and success/failure transitions racing a cancellation |
| `tests/unit/test_missions.py` | deterministic plan ordering/mode filtering/step cap, completion/failure/cancellation persistence with honest progress, terminal status summaries, idempotency, guild isolation, startup recovery (no nested transaction), moderator gate, disabled mode, service-level goal limit, shutdown cancellation |
| `tests/unit/test_research.py` | `safe_source_url` public-URL policy, Brave/Tavily result mapping and dedupe, provider enablement/key checks, model synthesis with URLs stripped, extractive fallback, unavailable/failed persistence, audit without query text, provenance to Content Studio with cross-guild/failed-report denial |
| `tests/unit/test_problem_radar.py` | minimum-data honesty, repeated question/problem evidence, approved-KB gap clearing, confidence scaling, no author fields, guild isolation |
| `tests/unit/test_content_planning.py` | event-plan template/archive guard/cross-guild, calendar draft/plan links, soft cancel and second-cancel refusal, reply-draft mention neutralization/bounds/dismissal/audit, non-human actor denial |
| `tests/unit/test_evaluation.py` | full offline evaluation passes and persists guild-scoped report + audit, honest disclaimer flags, isolation across guilds, permissive-skill regression fails the report; role roster, bounded dependency-aware planning, provider-availability honesty, agent-run idempotency/isolation, scheduler kind/idempotency/recovery checks |
| `tests/unit/test_ui_surfaces.py` | mission status/steps/references, failed/empty states, radar insufficient/full evidence, research unavailable vs cited, evaluation offline scope/failure, planning empty/linked states, memory status counts-only/source labeling |
| `tests/integration/test_live_ollama.py` | opt-in: real server reachable + model installed; one JSON round-trip that parses |

## Recorded runs

| Date | Command | Result |
|---|---|---|
| 2026-10-18 | `python -m pytest tests/unit tests/integration -q` | **213 passed, 2 skipped** (skips = opt-in live tests), exit 0 |
| 2026-10-18 | `python -m ruff check src tests` | **All checks passed** |
| 2026-10-19 | `.venv\Scripts\python.exe -m pytest tests/unit tests/integration -q` | **377 passed, 2 skipped** (skips = opt-in live-Ollama tests), exit 0 — includes the 29 Phase 3 regression tests |
| 2026-10-19 | `.venv\Scripts\python.exe -m ruff check --no-cache src tests scripts` | **All checks passed!** |
| 2026-10-22 | `python -m pytest` | **450 passed, 2 skipped, 1 warning** (skips = opt-in live-Ollama tests; warning = third-party `audioop` deprecation), exit 0 |
| 2026-10-22 | `python -m ruff check --no-cache src tests scripts` | **All checks passed!** |
| 2026-10-22 | `python -m pytest` | **479 passed, 2 skipped, 1 warning** after the program Phase 1 agent foundation; 29 new tests; exit 0 |
| 2026-10-22 | `python -m ruff check --no-cache src tests scripts` | **All checks passed!** |
| 2026-10-22 | `python -m pytest -o addopts=""` | **504 passed, 2 skipped, 1 warning** after the program Phase 2A approval state machine; 25 new tests; exit 0 |
| 2026-10-22 | `python -m ruff check --no-cache src tests scripts` | **All checks passed!** after Phase 2A |
| 2026-10-22 | `python -m pytest -o addopts=""` | **533 passed, 2 skipped, 1 warning** after the program Phase 2B Content Studio; 27 new Content Studio tests plus migration/count/bot/config updates; exit 0 |
| 2026-10-22 | `python -m ruff check --no-cache src tests scripts` | **All checks passed!** after Phase 2B; the new Content Studio files also pass `ruff format --check` (repo-wide `ruff format --check` still reports pre-existing formatting drift in older shared files) |
| 2026-10-22 | `python -m pytest -o addopts=""` | **562 passed, 2 skipped, 1 warning** after the Phase 3 Community Pulse + living knowledge base work; 29 new Phase 3 tests plus migration/retention/skill/command updates; exit 0 |
| 2026-10-22 | `python -m ruff check --no-cache src tests scripts` | **All checks passed!** after Phase 3 |
| 2026-10-22 | `python -m pytest -o addopts=""` | **574 passed, 2 skipped, 1 warning** after Phase 3.1 privacy/purge/operational hardening; 12 new tests plus embed/bot regressions; exit 0 |
| 2026-10-22 | `python -m ruff check --no-cache src tests scripts` | **All checks passed!** after Phase 3.1 |
| 2026-10-23 | `python -m pytest tests/unit/test_research.py tests/unit/test_missions.py tests/unit/test_problem_radar.py tests/unit/test_content_planning.py tests/unit/test_evaluation.py tests/unit/test_ui_surfaces.py -o addopts="" -q` | **62 passed in 5.02s**; two production bugs found and fixed (startup recovery nested transaction; unconditional planning transitions) |
| 2026-10-23 | `python -m pytest tests/unit tests/integration -o addopts="" -q` | **636 passed, 2 skipped, 4 warnings in 30.55s** (skips = opt-in live-Ollama; warnings = discord.py `asyncio.iscoroutinefunction` deprecation); exit 0 |
| 2026-10-23 | `python -m ruff check .` | **All checks passed!**; exit 0 |
| 2026-10-23 | final rerun after docs + `/mission` goal-range adjustment: `python -m pytest tests/unit tests/integration -o addopts="" -q` | **636 passed, 2 skipped, 4 warnings in 28.28s**; all relative Markdown links resolve; exit 0 |
| 2026-10-23 | after adding `/conversation-status`: `python -m pytest tests/unit tests/integration -o addopts="" -q`; `python -m ruff check .` | **642 passed, 2 skipped, 4 warnings in 31.10s**; Ruff clean; exit 0 |
| 2026-10-23 | gap closure (host agent, multimodal, moderation, voice, scheduler): `.venv/Scripts/python.exe -m pytest tests/unit tests/integration -o addopts="" -q -rs` | **698 passed, 2 skipped, 4 warnings in 39.42s**; skips = opt-in live-Ollama tests; warnings = discord.py `asyncio.iscoroutinefunction` deprecation; exit 0 |
| 2026-10-23 | gap closure lint: `.venv/Scripts/python.exe -m ruff check --no-cache src tests scripts` | **All checks passed!**; exit 0 |
| 2026-10-24 | final offline suite after specialist runtime closure: `.venv\Scripts\python.exe -m pytest -q` (JUnit counts cross-checked) | **753 passed, 2 skipped, 1 warning** (JUnit `tests=755`, `failures=0`, `errors=0`, `skipped=2`, 28.59s); skips = opt-in live-Ollama tests; warning = third-party `audioop` deprecation; exit 0 |
| 2026-10-24 | final lint on changed files: `.venv\Scripts\python.exe -m ruff check <changed files>`; `git diff --check` | **All checks passed!**; no whitespace errors; exit 0 |

## Manual verification (cannot be unit-tested here)

- Real Discord connectivity, command visibility, ephemeral behavior — checklist in
  [DEPLOYMENT.md](DEPLOYMENT.md) § Manual smoke test. **Partially performed**: live slash-command
  sessions and mention chat ran on 2026-10-19 (see PLAN.md test log); the remaining steps
  (including the Phase 3 steps 14–18) are still to be run on the demo machine.
- Phase 3.1 has a dedicated disposable-server checklist in [DEPLOYMENT.md](DEPLOYMENT.md)
  § Phase 3.1 manual Discord verification. It covers `/pulse`, `/pulse-draft`, candidate
  review and `/knowledge-purge`, including non-moderator and cross-guild attempts. **Not run
  in this phase**; no Discord connection was opened.
- Real model quality under demo conditions — Phase 0 benchmark numbers are recorded in
  [AI_SYSTEM.md](AI_SYSTEM.md) / [PLAN.md](PLAN.md).
- Phase 2A has no generic Discord approval command; Phase 2B adds the only executor and its Content Studio commands.
  Every transition and the migration are exercised only against temporary databases; no real
  `data/copilot.db` was opened, migrated, or backed up. The live `/content-*` workflow and a real
  Discord publish have **not been run**; publishing is disabled by default. A live run would need a
  scratch `DB_FILENAME`, a test guild/channel, `CONTENT_PUBLISHING_ENABLED=true`, and explicit
  confirmation that posting is acceptable.
- Missions, research, radar, evaluation and planning commands were **not invoked against live
  Discord** in the 2026-10-23 phase. Missions/radar/planning are covered by fake-provider and
  temporary-database tests; research used a fake search provider (no real Brave/Tavily call), and
  evaluation is offline by design. Live progress rendering, mission restart recovery, a real
  provider response and command sync remain in [E2E_TEST_RUNBOOK.md](E2E_TEST_RUNBOOK.md).
- **Consolidated live E2E: NOT RUN.** No bot login, slash-command sync, live provider call or
  real-user workflow was performed in this phase, by instruction.
