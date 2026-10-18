# Community Copilot — Architecture

## Design invariants (never violate)

1. **Discord handlers stay thin**: cogs do I/O + authorization + rendering; all logic lives in `core/`, `llm/`, `db/`, `util/`.
2. **All community content is untrusted data**: fenced, sanitized, bounded before prompts; model output is validated before rendering; nothing from content is ever executed, fetched, or used for authorization.
3. **The model has no capabilities**: no tools, no shell, no filesystem, no network beyond the Ollama HTTP call. It only transforms text → validated JSON.
4. **One inference at a time** (sequential queue): matches a single CPU-loaded model and makes latency/RAM predictable.
5. **Truthful docs**: `PLAN.md` distinguishes implemented vs verified; security claims exist only with a test (SECURITY.md).

## Module map

```
main.py                     entrypoint: load settings → logging → run bot
scripts/check_prereqs.py    environment verification (no side effects except a temp DB check)
src/copilot/
  config.py                 pydantic-settings; .env; typed limits; no secrets in reprs
  errors.py                 typed app errors mapped to friendly user messages
  bot.py                    discord client (no privileged intents), cog loading,
                            command sync (test guild if set), graceful shutdown
  cogs/                     Discord-facing layer (thin):
    health.py               /status      — truthful subsystem health
    summary.py              /summarize, /cancel — queue + rendering
    knowledge.py            /kb-search, /forget
    moderation.py           /resolve, /approve-solution, /reject-solution, /correct-status
    insights.py             /insights    — threshold-gated aggregates
    debug.py                /debug       — error/stack-trace explainer (dev IDs only)
  core/                     Discord-agnostic logic:
    collector.py            thread fetch with hard caps + skip accounting
    queue.py                sequential worker, capacity, cooldown, guild cap, cancel, timeout
    analyzer.py             collect → (images) → prompt → model → validate → result
    duplicate.py            FTS5 BM25 "possibly related" lookup
    solutions.py            propose/approve/reject workflow + audit
    insights_engine.py      aggregates with minimum-data gate
    images.py               attachment validation + safe re-encode
  llm/
    base.py                 LLMProvider protocol + dataclasses
    ollama_provider.py      AsyncClient wrapper: timeouts, keep_alive, JSON mode fallback
    capabilities.py         runtime capability check via /api/show (cached)
    prompts.py              system prompt, fenced content rendering, control-token stripping
    schemas.py              Pydantic models for all model outputs + JSON extraction
  db/
    database.py             stdlib sqlite3 via asyncio.to_thread, WAL, lock, migrations
    schema.sql              single source of truth: `-- @migration N` blocks
    repository.py           parameterized CRUD + FTS queries
    retention.py            sweep + per-thread purge (/forget)
  ui/
    embeds.py               embed builders with Discord length limits enforced
    checks.py               guild/channel allowlist + moderator logic (pure functions)
  util/
    logging.py              redacting formatter + secret registry
    text.py                 sanitize/truncate/fence helpers
tests/…                     unit (mocked) + opt-in live Ollama marker
data/                       runtime DB + logs (gitignored)
```

## Runtime data flow (`/summarize`)

```
interaction → allowlist check → queue.submit (capacity/cooldown/guild-cap)
  → worker: collector (thread history, caps, skip reasons)
           → images (if enabled + capability verified): validate → Pillow re-encode
           → prompts.build (fence + sanitize + char budget) 
           → provider.chat (timeout, JSON schema, keep_alive)
           → schemas: extract JSON → Pydantic validate → ≤1 repair retry
           → analyzer.sanitize: drop unknown source IDs, clamp lengths
  → embeds.build (limits) → interaction.followup (ephemeral) 
  → persist summary + fingerprint (Phase 5) → release worker
```

## Key interfaces

```python
class LLMProvider(Protocol):
    async def chat(self, *, system: str, user: str,
                   images: list[bytes] = (), json_schema: dict | None = None) -> str: ...
    async def health(self) -> ProviderHealth: ...        # reachable? version
    async def capabilities(self) -> ModelCapabilities: .. # exists? vision? ctx?

class InferenceQueue:      # core/queue.py
    async def submit(*, user_id, guild_id, run) -> Job     # raises QueueFull/Cooldown/GuildLimit
    def cancel(self, job_id, user_id) -> bool               # owner only; pending + active
    def snapshot(self) -> QueueSnapshot                     # for /status

class Database:            # db/database.py
    async def execute(sql, params) / fetchone / fetchall    # parameterized only
    migrations applied at connect from schema.sql blocks (user_version)
```

## Concurrency model

- discord.py asyncio loop; cogs await queue futures.
- Exactly **one** worker task runs model calls; everything else (DB, embeds) is cheap.
- DB: single connection + `asyncio.Lock` + `asyncio.to_thread`; WAL + `busy_timeout`.
- Timeouts: per fetch (`FETCH_TIMEOUT_S`), per model call (`OLLAMA_REQUEST_TIMEOUT_S`), per job (`JOB_TIMEOUT_S`).

## Why these choices (trade-offs)

| Choice | Alternative | Why chosen here |
|---|---|---|
| SQLite + FTS5 | vector DB + embeddings | RAM/CPU budget on an 8 GB CPU-only machine; zero extra services; lexical recall accepted and documented (RAG.md) |
| Sequential queue | concurrent inference | single CPU-loaded model; avoids RAM thrash and confusing latencies |
| stdlib sqlite3 + to_thread | aiosqlite | one less dependency; teaches the threading model; fine at this scale |
| JSON-schema-constrained output | free-form + regex | small models produce near-JSON; schema decoding + validation + repair is the reliability path |
| No privileged intents | Message Content intent | the bot never needs message events; least privilege |

## Extension points (future work)

- Embeddings + `sqlite-vec` behind `duplicate.py`'s interface (if lexical recall proves insufficient).
- Additional providers behind `LLMProvider` (any OpenAI-compatible local server).
- Per-guild settings table (currently `.env` allowlists).
