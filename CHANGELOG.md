# Changelog

All notable changes to Community Copilot. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project is pre-1.0 and versioning is
informal until a release tag exists.

## [0.1.0] — unreleased (build date 2026-10-18)

> Built and unit-tested end-to-end in one day; **nothing has been pushed to a remote**, and
> live Discord behavior is unverified (no token configured during the build). See
> [PLAN.md](PLAN.md) for the per-phase status truth.

### Phase 0 — Foundation
- Repo scaffold (Windows-first, `src/copilot` layout), pinned runtime + dev dependencies
  (`discord.py 2.7.1`, `ollama 0.6.3`, `pydantic 2.14.1`, `Pillow 13.0.0`, `httpx 0.28.1`).
- `scripts/check_prereqs.py` stdlib-only environment checker (Python, FTS5, Ollama, model
  capabilities, `.env`, disk, RAM).
- Docs set: README, PLAN, PRD, ARCHITECTURE, SECURITY, THREAT_MODEL, DEVELOPMENT,
  CONFIGURATION, TESTING, AI_SYSTEM.
- Phase 0 model benchmark with `gemma4:e2b` (raw evidence kept locally in `data/`):
  cold load ~34 s, warm ~4–6 s, ~16–28 tok/s CPU, JSON + `think:false` valid in ~4 s;
  empty output reproduced twice with thinking ON and a small token budget.

### Phase 1 — Secure skeleton
- `Settings` via pydantic-settings: token only from env as `SecretStr`; empty guild allowlist
  denies every guild; `validate_for_run()` refuses to start without required values.
- Redacting log formatter (registered secrets + token-shaped strings scrubbed from messages
  and tracebacks); content logging off by default.
- Authorization checks: guild/channel allowlists, moderator (permissions or role IDs),
  dev-user gate for `/debug`.
- `/status` truthful health panel (Discord latency, Ollama, model + capabilities, DB, queue).
- Graceful shutdown: DB close + inference worker cancellation.

### Phase 2 — `/summarize`
- Thread collector with hard caps: message cap (`MAX_MESSAGES`/`HARD_MAX_MESSAGES`), bounded
  scan window, character budget (oldest replies trimmed first, oversized opening truncated),
  structured skip notes surfaced in the final embed ("nothing silently dropped").
- Sequential inference queue: FIFO, pending-slot capacity, per-user cooldown, per-guild hourly
  cap, per-job timeout, `/cancel` (pending or running), cached capability/health checks.
- Analysis pipeline: fenced untrusted content → JSON-schema-constrained call → Pydantic
  validation → invented source-ID rejection → single bounded repair retry → friendly failure.
- Embed UI with clickable source-message links, status + confidence, "AI-generated — verify
  before acting" footer, Discord limit-aware paging.
- "Possibly related" detection (FTS5 BM25, lexical, always labeled not-verified).

### Phase 3 — Status corrections & persistence
- SQLite (stdlib) with WAL, foreign keys, single connection + asyncio lock, migration system
  driven by `PRAGMA user_version` (`schema.sql` `-- @migration N` blocks).
- Migration 1: `threads`, `messages`, `summaries`, `corrections`, `solutions`, `audit_log`.
- Moderator `/correct-status`; corrections override and annotate the stored record; audit rows.

### Phase 4 — Vision (capability-gated)
- Runtime capability probe (`/api/show`): vision features activate only if the installed
  model actually reports `vision`; otherwise the bot says images were skipped and why.
- Image pipeline: magic-byte format detection (PNG/JPEG/WebP only), byte + pixel caps,
  decompression-bomb guard, mandatory Pillow re-encode (metadata stripped), downscale to
  bound dimensions, JPEG fallback when the PNG copy exceeds the output cap; every skip is
  reported with its reason.

### Phase 5 — Knowledge base
- Migration 2: `kb_docs` + FTS5 external-content `kb_fts` with insert/update/delete triggers.
- Moderator workflow: `/resolve` (propose) → `/approve-solution` (promote into searchable KB)
  or `/reject-solution`; all transitions audited.
- `/kb-search` (BM25 ranking, body-column snippets, FTS-syntax-safe query building).
- Thread fingerprints indexed for duplicate surfacing; retrieval is display-only — never
  re-prompted (documented in RAG.md).
- Retention sweep (`RETENTION_DAYS`) and `/forget` purge including FTS rows.

### Phase 6 — Debug helper & insights
- `/debug` (dev-gated): explains pasted errors/stack traces; the embed states that nothing was
  executed, fetched or verified.
- `/insights` (moderator): recurring error signatures, status distribution, unanswered-thread
  counts — behind a minimum-data gate that refuses trend claims from too little data; no
  per-user tracking.

### Phase 7 — Hardening, portability, docs
- Fixed: shutdown deadlock caused by CPython 3.11 `asyncio.wait_for()` swallowing an external
  cancellation when the inner task finished in the same loop tick; worker now re-raises on
  post-job `_stopping`, skips cancelled jobs, and has a deterministic regression test.
- Fixed: collector now explicitly notes when an opening post could not be fetched at all
  (previously only noted when it existed but was unusable).
- Full unit suite: **213 passed, 2 opt-in live tests skipped**; `ruff check src tests` clean.
- Docs completed: TESTING, DEPLOYMENT (with recorded prereq-check output + troubleshooting),
  DEMO_GUIDE, RAG, CHANGELOG; SECURITY statuses updated to implemented+tested/manual;
  README command table aligned with reality.
