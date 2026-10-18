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
| `tests/unit/test_checks.py` | guild/channel allowlists, denial messages, moderator (permission + role), dev-user gate |
| `tests/unit/test_database.py` | migrations apply exactly once, WAL/FK pragmas, ping/close, transactional rollback |
| `tests/unit/test_queue.py` | capacity (pending-slot semantics), cooldown, guild cap, timeout, cancel pending + active, shutdown incl. the `wait_for` swallow regression |
| `tests/unit/test_repository.py` | parameterized CRUD, upserts, FTS5 search ranking + snippets, injection survivability, FTS trigger sync, audit JSON |
| `tests/unit/test_retention.py` | expiry math, full thread purge, idempotency, sweep leaves non-expired data |
| `tests/unit/test_solutions.py` | propose → approve (KB promotion + searchability) / reject, audit rows, not-found paths |
| `tests/unit/test_duplicate.py` | BM25 related-doc mapping, source-thread exclusion, limit, fingerprint text |
| `tests/unit/test_insights.py` | minimum-data gate, aggregates, malformed JSON tolerance, signature cleaning/caps, window math |
| `tests/unit/test_schemas.py` | JSON extraction (braces/escapes/garbage), status/confidence normalization, bounded lists, debug schema, flat decoding schemas |
| `tests/unit/test_prompts.py` | thread/message fencing, hostile content kept as inert data, repair + debug prompt shapes |
| `tests/unit/test_provider.py` | request shape (`think`, format, options), schema→`json` fallback, timeouts, 404 mapping, redaction, capability cache, `strip_thinking` |
| `tests/unit/test_collector.py` | opening/reply ordering, fetch fallback + note, skip accounting, dedup, caps, char budget, friendly errors |
| `tests/unit/test_images.py` | magic-byte sniffing, metadata stripping, downscale, pixel/byte caps, JPEG fallback, every skip reason |
| `tests/unit/test_analyzer.py` | happy path, invented-ID dropping, no-repair-on-empty-content, single repair retry (success/failure), vision gating |
| `tests/unit/test_embeds.py` | Discord limits, paging by fields and by text budget, mention neutralization, correction/related/notes sections, honest status wording |
| `tests/unit/test_health.py` | truthful per-subsystem status (down/missing/ok), capabilities not probed when unreachable, health never raises |
| `tests/integration/test_live_ollama.py` | opt-in: real server reachable + model installed; one JSON round-trip that parses |

## Recorded runs

| Date | Command | Result |
|---|---|---|
| 2026-10-18 | `python -m pytest tests/unit tests/integration -q` | **213 passed, 2 skipped** (skips = opt-in live tests), exit 0 |
| 2026-10-18 | `python -m ruff check src tests` | **All checks passed** |

## Manual verification (cannot be unit-tested here)

- Real Discord connectivity, command visibility, ephemeral behavior — checklist in
  [DEPLOYMENT.md](DEPLOYMENT.md) § Manual smoke test. **Not yet performed** (no token configured).
- Real model quality under demo conditions — Phase 0 benchmark numbers are recorded in
  [AI_SYSTEM.md](AI_SYSTEM.md) / [PLAN.md](PLAN.md).
