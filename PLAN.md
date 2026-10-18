# Community Copilot — Build Plan & Living Status

> Single source of truth for what is done, what is verified, and what is not.
> Updated after every phase. Legend: `[x]` done & verified · `[~]` implemented, live-verification pending · `[ ]` not started

## Environment snapshot (re-verified 2026-10-18, Windows)

| Fact | Value |
|---|---|
| Project dir | `C:\Users\VISHAL KUMAR\community-copilot` (was empty at start; git initialized 2026-10-18, branch `main`, **no remote, nothing pushed**) |
| Python | 3.11.9; venv at `.venv` (pip 26.2.1) |
| SQLite | 3.45.1 via stdlib; **FTS5 verified** (`CREATE VIRTUAL TABLE ... USING fts5` succeeds) |
| Ollama | client+server **0.40.2**; model **`gemma4:e2b` already on disk** (4.27 GB total: 3.26 GB weights + 0.92 GB vision projector + 0.09 GB MTP draft; `ollama list` size 4.6 GB) — **no download needed**; `Q4_K_M`, 4.6B params |
| Model capabilities | **VERIFIED via `/api/show` (2026-10-18): `completion, vision, audio, tools, thinking`; context length 131072** → vision feature (Phase 4) is natively supported; tools capability is deliberately unused (no tools given to the model) |
| Phase 0 benchmark | **done** — cold load ~34 s, warm ~4–6 s, ~16–28 tok/s CPU, JSON+`think:false` valid in ~4 s; thinking ON with small `num_predict` returned empty content twice (evidence: `data/phase0_chat_*.json`) |
| Ollama server | Tray app exists but its server died after startup (GPU-discovery watchdog timeouts in `%LOCALAPPDATA%\Ollama\server.log`). A single headless `ollama serve` was started for this build; see TROUBLESHOOTING in DEPLOYMENT.md |
| Hardware | Intel UHD 730 (no NVIDIA GPU → CPU inference), 7.8 GB total RAM, ~88 GB free disk (C:) |
| Discord | Not configured yet (no token in `.env`) → **live Discord tests are unverified by design; all automated tests use mocks** |

## Phase tracker

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundation: repo, venv, pinned deps, docs, prereq script, model/latency benchmark | `[x]` |
| 1 | Secure bot skeleton: config, logging/redaction, allowlists, `/status`, graceful errors | `[~]` implemented + unit-tested (`test_config`, `test_redaction`, `test_checks`, `test_queue`, `test_health`); live Discord unverified (no token) |
| 2 | `/summarize`: collection, sequential queue, timeouts, cancellation, embeds | `[~]` implemented + unit-tested (`test_collector`, `test_analyzer`, `test_embeds`, `test_queue`); live Discord unverified |
| 3 | Issue status + moderator corrections (DB foundation) | `[~]` implemented + unit-tested (`test_database`, `test_repository`, `test_schemas`); live Discord unverified |
| 4 | Image/screenshot analysis (capability-gated) | `[~]` implemented + unit-tested (`test_images`, `test_analyzer`); live Discord unverified |
| 5 | Knowledge base: SQLite FTS5, `/kb-search`, duplicates, retention, `/forget` | `[~]` implemented + unit-tested (`test_repository`, `test_solutions`, `test_retention`, `test_duplicate`); live Discord unverified |
| 6 | `/debug` error explainer + `/insights` (data-threshold gated) | `[~]` implemented + unit-tested (`test_analyzer`, `test_insights`, `test_embeds`); live Discord unverified |
| 7 | Security tests, hardening, portability, demo prep, final docs | `[~]` in progress: docs pass complete except remaining manuals (DEMO_GUIDE, DEPLOYMENT, RAG, CHANGELOG — written this session) |

## Decisions log

| # | Decision | Why |
|---|---|---|
| D1 | No privileged Discord intents; threads fetched via REST on demand | Least privilege; no Message Content intent needed |
| D2 | SQLite + FTS5 only (no vector DB, no embeddings) | Local-first, RAM-frugal, dependency-light; trade-off documented in RAG.md |
| D3 | Model `gemma4:e2b` (already present) — final confirmation after Phase 0 benchmark | Vision-capable, edge-optimized; must fit 7.8 GB RAM in practice |
| D4 | Thinking mode OFF by default; JSON-schema-constrained output | Deterministic extraction, lower CPU/latency budget |
| D5 | Single sequential inference worker | One CPU-loaded model; predictable memory and rate behavior |
| D6 | Migrations = `schema.sql` blocks parsed by `user_version` | One source of truth for schema history |
| D7 | No aiosqlite: stdlib `sqlite3` via `asyncio.to_thread` + one connection + lock | Zero extra deps; teaching-friendly |
| D8 | Headless `ollama serve` started manually for dev; user can stop it anytime | Tray app server was down/crashed; needed for live verification |
| D9 | Queue capacity counts only *pending* jobs; the active job is accounted separately | The single worker must never block itself; `/cancel` and capacity stay predictable |
| D10 | Duplicate detection = FTS5 BM25 lexical only, labeled "possibly related (not verified)" | No embeddings/vector DB on an 8 GB CPU machine; precision over recall |
| D11 | Worker treats any post-job `_stopping` as cancellation (explicit re-raise) | CPython 3.11 `asyncio.wait_for()` can swallow an external cancel when the job task finished in the same loop tick — caused a real shutdown deadlock; fixed with a regression test |
| D12 | `MAX_CHARS` default kept conservative (24000) until demo-machine profiling | CPU inference at ~16–28 tok/s punishes oversized prompts |

## Blockers / external dependencies

| Blocker | Impact | Unblock steps |
|---|---|---|
| No Discord token configured | Live Discord commands can't be tested here; all tests are mocked | User creates app + test server, writes token to `.env`, runs `check_prereqs.py`, then manual smoke test (DEPLOYMENT.md) |
| Presentation laptop hardware unknown | Model choice for demo may differ | Run `scripts/check_prereqs.py` there; guide covers 8 GB and 16 GB classes |

## Test log

| Date | Phase | Command | Result |
|---|---|---|---|
| 2026-10-18 | 0 | `ollama run gemma4:e2b` plain + JSON smoke tests (raw JSON saved to `data/`) | cold load ~34 s; warm ~4–6 s; ~16–28 tok/s; JSON valid in ~4 s with `think:false`; empty content twice with thinking ON + small `num_predict` |
| 2026-10-18 | 1–6 | `.venv\Scripts\python.exe -m pytest tests/unit tests/integration -q` | **213 passed, 2 skipped** (skips = opt-in live-Ollama tests, `RUN_LIVE_OLLAMA=1` to enable); exit 0 |
| 2026-10-18 | 1–6 | `.venv\Scripts\python.exe -m ruff check src tests` | **All checks passed** (line-length 120, `E,F,W,I,UP,B,ASYNC`, UP042 ignored intentionally) |
| 2026-10-18 | 1 | queue shutdown regression | Repro'd: `Task.cancel()` swallowed by 3.11 `wait_for()` (`Lib/asyncio/tasks.py:478`) when inner task completed same tick → worker stuck on `queue.get()` while `stop()` awaited it. Fixed + regression test `test_stop_survives_wait_for_swallowing_cancellation` |
| pending | live | manual Discord smoke test | requires a token in `.env` (see DEPLOYMENT.md) — **not yet performed, by design** |

## Known limitations (living list)

- Duplicate detection is lexical (FTS5 BM25), not semantic; paraphrased duplicates may be missed (documented in RAG.md).
- `gemma4:e2b` output quality is bounded by size; all model output is validated and labeled as suggestions, never as fact.
- Windows-first: paths use `pathlib`; Linux/macOS untested but intended to work.
- Live Discord behavior (gateway, permission edge cases) is unit-tested through duck-typed fakes but **not yet exercised against real Discord** in this build (no token configured).
- Test suite is mock-only by design; the single live-Ollama integration test is opt-in and was not run in the recorded log above.
