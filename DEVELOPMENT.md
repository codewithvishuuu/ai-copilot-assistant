# Community Copilot — Development Guide

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # includes runtime deps
Copy-Item .env.example .env
```

`.venv` and `.env` are gitignored. Never install into the system Python for this project.

## Everyday commands

| Task | Command |
|---|---|
| Run tests | `.venv\Scripts\python.exe -m pytest` |
| Run only unit tests | `.venv\Scripts\python.exe -m pytest tests/unit` |
| Lint | `.venv\Scripts\ruff.exe check src tests scripts` |
| Format check | `.venv\Scripts\ruff.exe format --check src tests scripts` |
| Verify environment (no secrets needed) | `.venv\Scripts\python.exe scripts\check_prereqs.py` |
| Run the bot | `.venv\Scripts\python.exe main.py` |
| Live Ollama tests (opt-in) | `$env:RUN_LIVE_OLLAMA="1"; .venv\Scripts\python.exe -m pytest -m live_ollama` |

## Project layout

See [ARCHITECTURE.md](ARCHITECTURE.md). The rule that matters:

- `cogs/` — thin Discord layer (I/O, authorization calls, rendering only).
- `agents/` — explicit typed read-only skill registry/orchestrator; no model tool surface.
- `core/`, `llm/`, `db/`, `util/` — pure logic, testable without Discord.

## Conventions

- Python 3.11+, full type hints on public functions.
- Pydantic models for anything crossing a boundary (config, model output, DB rows).
- Every community-content string passes through `util/text.py` helpers before entering a prompt.
- New user-visible failure paths must map to a friendly message in `errors.py` (never raw tracebacks).
- Log at INFO without message bodies; use `DEBUG_LOG_CONTENT=true` only locally.
- Tests: no network, no real Discord, no real Ollama unless marked `live_ollama`.

## Adding a slash command (recipe)

1. Create/extend a cog in `src/copilot/cogs/`.
2. At the top of the handler: `checks.ensure_allowed(interaction)`.
3. Use the shared `InferenceQueue` for queued work (summary/debug); chat and Content Studio use their own timeouts plus the provider-level semaphore. Never call `provider.chat` directly from a cog.
4. Render with `ui/embeds.py` builders (they enforce Discord limits).
5. Add unit tests with fake interactions/providers.

## Adding a schema migration (recipe)

1. Append a new `-- @migration N` block to `src/copilot/db/schema.sql`; never edit an applied block.
2. Add the new table to `_KNOWN_TABLES` in `db/database.py` if health counts should include it.
3. Add repository methods with parameterized SQL and conditional updates for state changes.
4. Update `test_migrations.py` (v2 → current, additive objects, data/FTS preserved) and any
   version/count assertions; never point tests at the real `data/copilot.db`.

## Privileged actions

- New side effects must use `core/approvals.py`; do not create a parallel approval path.
- Call `begin_execution_claim` immediately before the side effect, revalidate the target and actor,
  record success only with external evidence, and reconcile ambiguous outcomes by evidence rather
  than retrying. `core/publishing.py` is the reference implementation.

## Git hygiene

- Commit small and often; `.env`, `data/`, `.venv/` must never appear in `git status` as staged.
- Before committing: `git status --short` and skim `git diff` for accidental secrets.
- No remote pushes without the maintainer's explicit go-ahead.
