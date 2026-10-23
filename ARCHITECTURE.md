# Community Copilot — Architecture

## Design invariants (never violate)

1. **Discord handlers stay thin**: cogs do I/O + authorization + rendering; all logic lives in `core/`, `llm/`, `db/`, `util/`, and the typed `agents/` skill layer.
2. **All community content is untrusted data**: fenced, sanitized, bounded before prompts; model output is validated before rendering; nothing from content is ever executed, fetched, or used for authorization.
3. **The model has no capabilities**: no tools, no shell, no filesystem, no network beyond the Ollama HTTP call. It only transforms text → validated JSON.
4. **Bounded inference**: the sequential queue runs one slash-command job at a time (matches a single CPU-loaded model); mention chat is serialized separately by its own single in-flight gate — it never occupies a queue slot and never enqueues a job. Both paths share one provider-level semaphore (`OLLAMA_MAX_CONCURRENT`, default 2 = one job + one chat), so simultaneous model calls are a configured bound, not an accident of wiring.
5. **Truthful docs**: `PLAN.md` distinguishes implemented vs verified; security claims exist only with a test (SECURITY.md).
6. **Conversation isolation is application code**: prompts contain only the calling user's own turns for the current channel/thread; neither the model nor user text can widen or choose a scope, and every read is bounded (turns/chars, per-scope cap, retention age).
7. **Tenant isolation is application code**: one process may serve several allowlisted guilds, but every explicit target thread must belong to the interaction guild and pass the channel allowlist. Knowledge-base search, related-item lookup, solution decisions, `/forget`, and insight aggregates are guild-scoped in the data layer, never by prompt or UI convention.
8. **Approval is an explicit stored state**: only `kb_docs.approved=1` documents are eligible for chat grounding. Auto-indexed thread fingerprints (`approved=0`) are display-only related history, never approved knowledge.
9. **Agent skills are application code, not model tools**: Phase 1 registers only typed, read-only skills behind an explicit registry. The deterministic orchestrator checks guild/channel scope and declared permissions before execution, enforces per-skill time/output bounds, and returns structured safe errors. The model never chooses or invokes a skill, and no public-action capability is registered.
10. **Privileged actions are backend approval state**: Phase 2A stores a request's guild, action, target, content version and SHA-256 hash (never the content), plus requester and expiry. Only a trusted human actor with the `decide` permission can approve/reject, self-approval is refused, and every transition is conditional. Phase 2B adds one concrete executor, Discord content publishing: it must claim the exact approved target/version by revalidating human authority, state, expiry and binding immediately before one adapter send; claims are unique per request and idempotency key, and all transitions are audited. There is still no generic executor command or model tool.

## Module map

```
main.py                     entrypoint: load settings → logging → run bot
scripts/check_prereqs.py    environment verification (no side effects except a temp DB check)
src/copilot/
  config.py                 pydantic-settings; .env; typed limits; no secrets in reprs
  errors.py                 typed app errors mapped to friendly user messages
  bot.py                    discord client (no privileged intents), cog loading,
                            command sync (test guild if set), quiet prefix handling,
                            graceful shutdown
  cogs/                     Discord-facing layer (thin):
    health.py               /status      — truthful subsystem health
    summary.py              /summarize, /cancel — queue + rendering
    knowledge.py            /kb-search, candidate review, /knowledge-purge, /forget
    moderation.py           /resolve, /approve-solution, /reject-solution, /correct-status
    insights.py             /insights    — threshold-gated aggregates
    debug.py                /debug       — error/stack-trace explainer (dev IDs only)
    conversation.py         @mention chat listener + /clear-conversation (non-privileged events)
    content.py              /content-* Content Studio + approval inbox and buttons (moderators)
    pulse.py                /pulse, /pulse-draft (moderators; draft only, no publishing)
  agents/                   Phase 1 read-only skill foundation (no model tools):
    contract.py             typed Skill contract, Pydantic request/input/output, limits, permissions
    registry.py             explicit IDs; rejects duplicates, arbitrary callables, non-read-only skills
    orchestrator.py         guild/channel auth, permission check, timeout/output bounds, safe errors
    builtin.py              kb.search, insights.community, pulse.community,
                            knowledge.candidates (all read-only; moderator-gated where needed)
  core/                     Discord-agnostic logic:
    collector.py            thread fetch with hard caps + skip accounting
    queue.py                sequential worker, capacity, cooldown, guild cap, cancel, timeout
    analyzer.py             collect → (images) → prompt → model → validate → result
    duplicate.py            FTS5 BM25 "possibly related" lookup
    solutions.py            propose/approve/reject workflow + audit
    knowledge_candidates.py candidate source validation, dedup/contradiction hints, review gate
    knowledge_purge.py      explicit one-document approved-KB purge (human + decide, guild-scoped)
    community_pulse.py      deterministic evidence-backed observations + estimated recommendations
    approvals.py            Phase 2A typed approval state machine + execution claims
    content_studio.py       Phase 2B drafts, immutable versions, generation, validation, review views
    publishing.py           sole content executor: approval claim + one adapter send or nonce recovery
    insights_engine.py      aggregates with minimum-data gate
    images.py               attachment validation + safe re-encode
    conversation.py         chat trigger policy + bounded context store
    chat_memory.py          durable chat turns (SQLite, scoped) with in-memory fallback
  llm/
    base.py                 LLMProvider protocol + dataclasses
    ollama_provider.py      AsyncClient wrapper: timeouts, keep_alive, JSON mode fallback
    capabilities.py         runtime capability check via /api/show (cached)
    prompts.py              system prompt, fenced content rendering, control-token stripping
    persona.py              conversation personality prompt (versioned) + static replies
    schemas.py              Pydantic models for all model outputs + JSON extraction
  db/
    database.py             stdlib sqlite3 via asyncio.to_thread, WAL, lock, migrations
    schema.sql              single source of truth: `-- @migration N` blocks
    repository.py           parameterized CRUD + guild-scoped FTS queries + conversation turns
    retention.py            sweeps + guild-scoped per-thread purge (/forget) + chat-turn age sweep
  ui/
    embeds.py               embed builders with Discord length limits enforced
    checks.py               guild/channel + target-thread + moderator logic (pure functions)
    content.py              Discord target/permission checks, send, nonce-based reconciliation
  util/
    logging.py              redacting formatter + secret registry
    text.py                 sanitize/truncate/fence helpers
tests/…                     unit (mocked) + opt-in live Ollama marker
data/                       runtime DB + logs (gitignored)
```

## Agent request flow (Phase 1)

```
TaskRequest (explicit skill_id + bounded input)
  → SkillOrchestrator: guild/channel allowlist + declared permission check
  → SkillRegistry.lookup (unknown IDs stop here; no arbitrary callables)
  → skill.input_model validation (extra fields rejected)
  → skill.execute (existing repository/insights service; timeout enforced)
  → skill.output_model validation + serialized-size bound
  → SkillResult (status, output, references, safe error)
```

There is no model-generated skill name and no model tool surface. `kb.search` calls the
guild-scoped, `approved_only=True` repository query; `insights.community` calls the existing
guild-scoped insights engine. Thread summarization and `/debug` are intentionally not adapted
yet because they depend on a live Discord thread or a separate dev-only boundary.

## Approval flow (Phase 2A)

```
trusted human + decide permission
  → approve/reject: conditional pending → approved/rejected (guild-scoped; requester ≠ approver)
approved request + executor permission
  → begin_execution: revalidate state + expiry + exact action/target/content version/SHA-256
  → conditional approved → executing + insert unique execution claim (idempotency key)
  → registered executor performs side effect (Phase 2B: content publishing only)
  → finish_execution: conditional executing → executed/failed + audit
```

The request/decision/claim/outcome writes and their audit rows share one SQLite transaction.
Missing, rejected, expired, cancelled, mismatched, already-claimed or already-finished requests
fail closed. A changed target or content cannot reuse an old approval because the exact binding
is part of the conditional claim. `ApprovalActor` permissions are supplied by application code,
never by model output. Expiry is also swept from the existing daily retention loop; approval and
audit rows are retained as evidence and are not purged by thread/conversation retention.

## Content Studio flow (Phase 2B)

```
moderator /content-create
  → bounded approved-only KB context (untrusted, fenced) → model draft JSON
  → validation (required text, length, control characters, mass mentions, exact-hash duplicate warning)
  → persist draft v1 (text is stored locally for review); needs_research blocks submission
moderator /content-submit
  → fresh target resolution (same guild, allowlist, exists, bot send permission)
  → Phase 2A request (requester = current version author, target + SHA-256 of exact version)
another moderator /content-approve (or buttons)
  → only the exact pending version/target can be approved; edits/target changes create a new
    immutable version, cancel the old approval, and hide the old buttons
moderator /content-publish
  → SERVER-SIDE: human + execute rights, fresh Discord moderator rights, guild/channel allowlist
  → re-resolve target and bot send permission, re-read approval state/expiry/binding
  → Phase 2A claim (approved → executing, unique) → one Discord send
  → success recorded only with the returned message id; definite failure → failed
  → timeout/5xx/unknown → leave executing; /content-recover searches for the nonce and records
    success only on exact evidence, otherwise leaves the claim untouched (never resends)
```

Draft/version/audit rows are retained as review evidence; existing retention sweeps do not purge
them. Publishing is off by default and must be enabled by the operator.

## Community Pulse and living knowledge (Phase 3)

```
/pulse (moderator)
  → insights engine minimum-data gate (no output below the threshold)
  → guild-scoped summaries only (no author/member columns in the query)
  → observations: status counts, unresolved threads, repeated questions,
    recurring error signatures, common thread-name words, BM25 similar discussions
  → recommendations: kind + evidence pointers + rationale + next action +
    "Estimate (not measured)" outcome + uncertainty (hash-stable id)

/pulse-draft <recommendation id> <channel>
  → recompute Pulse for the caller's guild/window/threshold
  → reject an id that is no longer reproducible
  → ContentStudio.create_draft(...) ONLY (no submit/approve/schedule/publish)

/knowledge-candidate-propose <summary id>
  → discover candidate text from that guild-owned summary
  → validate thread + summary + guild; detect identical/overlapping candidates
  → insert solutions(state='proposed') + audit; never a kb_docs row
/knowledge-candidate-approve|reject (moderator)
  → human + decide permission, guild-scoped lookup
  → existing solutions approve/reject transaction (conditional on state='proposed')
  → approval is the only path that inserts an approved kb_docs row

/knowledge-purge <doc N> (moderator)
  → fresh Discord moderator check + trusted human decide actor
  → guild-scoped approved kb_docs lookup
  → DELETE row + audit row in one transaction (FTS trigger follows)
  → failure rolls both back; audit stores no title/url/body
```

The model does not select skills, create candidates, or approve anything. Candidate
creation and review are slash commands whose authorization comes from Discord member
state, never from generated text.

## Purge and audit (Phase 3.1)

```
/knowledge-purge <doc N> (moderator)
  → allowlist + fresh moderator check; ApprovalActor(kind=human, decide)
  → SELECT approved kb_docs WHERE id = ? AND guild_id = actor.guild_id
  → DELETE row + INSERT audit row in one transaction
  → FTS external-content trigger removes the index row
  → failure rolls back both; a concurrent second purge sees not_found
```

`/kb-search` now displays `doc N` and approved/unreviewed state so a moderator can
locate the exact target. The audit record stores ids, kind, source ref and body length;
title, URL and body are never copied. Audit rows are append-only and retained
indefinitely — see [AUDIT.md](AUDIT.md). Automatic thread expiry still preserves approved
`kb_docs`; `/knowledge-purge` is the explicit one-document exception, `/forget` the
full-thread path. A solution-backed document purge leaves the solution row as historical
evidence, so re-publishing requires a new candidate.

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

## Runtime data flow (mention chat)

```
message mentioning the bot → allowlist check → trigger policy (mention / resolved reply)
  → history: recent turns for (guild, channel, user) from SQLite — database-first, bounded
    by turn/char budget (the bounded in-memory store is used only while the DB is unavailable)
  → images (if any): safe pipeline only (magic bytes + re-encode, vision capability verified)
  → admission: one in-flight chat generation; ≤ CONVERSATION_MAX_WAITING requests may wait;
    beyond that the requester gets one rate-limited "busy" notice (no unbounded waiter pile-up)
  → provider.chat_messages (typing indicator, timeout, token cap, shared OLLAMA_MAX_CONCURRENT slot)
  → persist the exchange (one transaction; per-scope row cap pruned in the same transaction;
    nothing is stored for failed or cancelled generations)
  → sanitize (mentions neutralized) → fence-safe chunks ≤ 1900 chars → reply in place
```

Chat turns persist across restarts (migration 3, `conversation_turns`), strictly scoped per
guild/channel/user; `/clear-conversation` deletes the caller's own turns in one channel. Reads
always prefer the database, so retention sweeps and clears can never be resurrected by the
in-memory fallback; a database failure degrades chat to in-process context only (logged). A
reply to the bot that Discord does not expose as readable text gets a rate-limited "mention me"
hint instead of a guess.

Delivery is honest: the exchange is persisted before sending; sending stops at the first failed
chunk (no duplicate continuations, no half-mangled replies); each failure is logged once with its
chunk number and a redacted reason; and both the log line and the handler's return value report
the truthful `delivered=` state (a failed persistence is reported as `persisted=False`, never as
stored). Throttled and at-capacity requests get at most one short model-free notice per 5 minutes;
a request for a scope whose reply is already generating is skipped silently (the typing indicator
already shows that work is in progress).

## Key interfaces

```python
class LLMProvider(Protocol):
    async def chat(self, *, system: str, user: str,
                   images: list[bytes] = (), json_schema: dict | None = None) -> str: ...
    async def chat_messages(self, messages: list[dict], *, images: list[bytes] = (),
                            timeout_s: float | None = None, max_tokens: int | None = None) -> str: ...
    async def health(self) -> ProviderHealth: ...        # reachable? version
    async def capabilities(self) -> ModelCapabilities: .. # exists? vision? ctx?

class InferenceQueue:      # core/queue.py
    async def submit(*, user_id, guild_id, run) -> Job     # raises QueueFull/Cooldown/GuildLimit
    def cancel(self, job_id, user_id) -> bool               # owner only; pending + active
    def snapshot(self) -> QueueSnapshot                     # for /status

class Database:            # db/database.py
    async def execute(sql, params) / fetchone / fetchall    # parameterized only
    migrations applied at connect from schema.sql blocks (user_version)

class Skill:               # agents/contract.py (ABC; typed input/output)
    skill_id: str; input_model; output_model; permissions; limits; read_only: bool
    async def execute(context: SkillContext, payload) -> SkillOutput: ...

class SkillOrchestrator:   # agents/orchestrator.py
    async def run(request: TaskRequest, context: SkillContext) -> SkillResult
```

## Concurrency model

- discord.py asyncio loop; cogs await queue futures.
- Model calls: one provider-level semaphore (`OLLAMA_MAX_CONCURRENT`, default 2) bounds **all**
  simultaneous Ollama generation calls. Health/capability probes are short and deliberately not gated.
- The queue runs exactly **one** worker task for slash-command jobs. Mention chat is
  separate: one in-flight gate (no queue slots, no queue cooldown), so one short chat
  call can overlap one queued job — sized deliberately for interactive replies.
- Chat waiters are bounded by `CONVERSATION_MAX_WAITING` (default 2): the `locked()` check and
  `acquire()` run without an await in between, so the waiter count cannot be raced past its bound.
  Beyond it, a request is rejected *before* doing any work and gets one rate-limited "busy"
  notice. Waiting requests each hold a live typing indicator — exactly why the bound exists.
- Fairness: a chat reply takes at most one of the (≥ 2 by default) provider slots, so the queue
  worker always finds a free slot — repeated chat messages cannot starve `/summarize`/`/debug`,
  and the chat in-flight gate (1) keeps chat generations from competing with each other. With
  `OLLAMA_MAX_CONCURRENT=1` everything serializes by design (documented trade-off); a chat reply
  then waits behind a job, bounded by `JOB_TIMEOUT_S`, and time spent waiting for a slot is not
  counted against the model-call timeout.
- Cancellation: the in-flight gate and the provider slot are released in `finally` blocks on every
  exit (success, error, timeout, cancel); a cancelled request stores nothing, sends nothing, and
  leaves no permanent busy state.
- Everything else (DB, embeds) is cheap.
- DB: single connection + `asyncio.Lock` + `asyncio.to_thread`; WAL + `busy_timeout`.
  Chat exchanges are written in one transaction (insert pair + per-scope prune); reads use the
  scope index and are bounded (prompt limit × 2 rows). No DB transaction is ever held across a
  model or Discord await.
- Timeouts: per fetch (`FETCH_TIMEOUT_S`), per model call (`OLLAMA_REQUEST_TIMEOUT_S`), per job (`JOB_TIMEOUT_S`).

## Why these choices (trade-offs)

| Choice | Alternative | Why chosen here |
|---|---|---|
| SQLite + FTS5 | vector DB + embeddings | RAM/CPU budget on an 8 GB CPU-only machine; zero extra services; lexical recall accepted and documented (RAG.md) |
| Sequential queue | concurrent inference | single CPU-loaded model; avoids RAM thrash and confusing latencies |
| stdlib sqlite3 + to_thread | aiosqlite | one less dependency; teaches the threading model; fine at this scale |
| JSON-schema-constrained output | free-form + regex | small models produce near-JSON; schema decoding + validation + repair is the reliability path |
| One shared model semaphore | per-call ad-hoc concurrency | simultaneous generation becomes a configured bound (default 2 = job + chat); changing it is one env var with a documented RAM/latency cost |
| Stop sending after the first failed chunk | retry every chunk | a partial delivery cannot grow duplicates or arrive out of order; the failure is logged once and reported as `delivered=False` |
| No privileged intents | Message Content intent | message events (non-privileged) power mention chat; with Message Content off Discord exposes text only when the bot is mentioned — least privilege preserved |

## Extension points (future work)

- Embeddings + `sqlite-vec` behind `duplicate.py`'s interface (if lexical recall proves insufficient).
- Additional providers behind `LLMProvider` (any OpenAI-compatible local server).
- Per-guild settings table (currently `.env` allowlists).
- Phase 2B content executor is the reference pattern for any future approved action: resolve the target immediately before claiming, recheck the human actor's current authority, claim the exact binding, perform one adapter call, and reconcile ambiguous outcomes by evidence only. There is deliberately no generic executor.
- Future read-only or explicitly approval-gated skills, each with its own auth/data tests.
