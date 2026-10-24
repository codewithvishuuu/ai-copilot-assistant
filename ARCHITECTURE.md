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
11. **Missions are fixed, read-only plans**: a mission mode maps to a deterministic ordered list of existing skills. The model never selects a mode, step, skill or target; every mission is human-started, moderator-gated, guild-scoped, bounded by step count/time/output caps, cancellable between steps, and audited. A restart closes interrupted missions as `failed`/`interrupted` rather than leaving them `running`.
12. **Research citations come only from provider results**: web research performs search-API queries only (no page fetch), validates every URL as public http(s) without credentials, and strips model-produced URLs before display. Disabled, unconfigured or failing research is reported as `unavailable`/`failed`, never as fabricated evidence.
13. **Draft-only planning surfaces**: event plans, calendar entries and reply drafts never send, schedule or post anything. They are guild-scoped, moderator-only, persisted for review, and transition one-way under conditional updates.
14. **Evaluation is offline and honest about scope**: `/evaluate` runs deterministic contract/safety/isolation/regression checks on temporary databases only, never contacts Discord/Ollama/search providers, and its report explicitly states that model quality was not evaluated.
15. **The host agent plans, it does not act**: `/agent` turns a goal into a bounded dependency graph over registered read-only skills. Independent steps run under `AGENT_MAX_PARALLEL`, dependents receive upstream outputs, and partial failures are reported step-by-step. The host cannot select an unregistered skill, publish, approve or moderate; cancellation is cooperative and a restart closes interrupted runs honestly.
16. **Attachments and extracted text are untrusted evidence**: format is decided by magic bytes, not by filename or MIME hint. Files are bounded by count/size/pages/pixels and re-encoded or extracted safely; the bot never executes code or commands from attachments, never stores whole images/documents, and answers distinguish observations from inferences.
17. **Scheduled means durable and non-publishing**: scheduler jobs live in SQLite, are claimed atomically, retried with backoff and recovered after restart. The only job kinds are a read-only agent run, a read-only digest and an approval-gated Content Studio draft. There is no scheduled-publishing kind, and enabling the scheduler never bypasses the approval state machine.

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
    conversation.py         @mention chat listener + /conversation-status + /clear-conversation
                            (non-privileged events; status shows counts, never content)
    content.py              /content-* Content Studio + approval inbox and buttons (moderators)
    pulse.py                /pulse, /pulse-draft (moderators; draft only, no publishing)
    missions.py             /mission, /missions, /mission-view, /mission-cancel, /mission-control
    research.py             /research, /research-draft (moderators; provider off by default)
    radar.py                /radar (moderator; evidence-backed read model)
    evaluation.py           /evaluate, /evaluation-latest (configured devs; offline only)
    planning.py             /calendar*, /event-plan*, /reply-* (moderators; inert drafts only)
    agents.py               /agent*, /agent-control (moderators; read-only host plans + cancel)
    automod.py              AI moderation listener, policy commands, human review modal
    scheduler.py            /schedule-*, /scheduled-jobs, /schedule-cancel, /calendar-schedule
  missions/                 fixed read-only multi-step plans:
    planner.py              mode → deterministic ordered steps over registered skill IDs
    service.py              persistence, progress, cancellation, timeouts, recovery, audit, views
    models.py               plan/step/result Pydantic models and status enums
  research/                 web research (search-API only):
    provider.py             Brave/Tavily search clients, bounded results, URL safety, no page fetch
    service.py              query → search → bounded synthesis (URL-stripped) or extractive fallback,
                            persisted report + audit + Content Studio provenance
  agents/                   read-only skill foundation (no model tools):
    contract.py             typed Skill contract, Pydantic request/input/output, limits, permissions
    registry.py             explicit IDs; rejects duplicates, arbitrary callables, non-read-only skills
    orchestrator.py         guild/channel auth, permission check, timeout/output bounds, safe errors
    builtin.py              kb.search, insights.community, pulse.community,
                            knowledge.candidates, research.web (all read-only; gated per skill)
    roles.py                eight specialist roles, each mapped to at least one
                            registered runtime skill, with permissions and
                            human-approval requirements
    host.py                 goal → bounded dependency-aware plan, bounded-parallel execution,
                            upstream output passing, cancellation, recovery, guild-scoped views
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
    problem_radar.py        deterministic read-only projection over Community Pulse
    content_planning.py     event plans, informational calendar and reply drafts (never sent)
    attachments.py          magic-byte validation, bounded PDF/DOCX/text extraction, retention scope
    moderation_ai.py        guild policy, context classification, incidents, review, retry idempotency
    content_voice.py        guild voice profile, approved history, feedback reuse, near-duplicate checks
    scheduler.py            durable SQLite jobs: atomic claim, backoff retry, restart recovery, no publish
    evaluation.py           offline contract/safety/isolation/regression checks + persisted report
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
    missions.py             mission progress/result/control embeds; empty/unavailable states
    agents.py               host run/list/control embeds with step status and evidence
    scheduler.py            scheduled-job embeds; always states that nothing posts or publishes
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

## Mission orchestration (program Phase 3.2)

```
moderator /mission <mode> <goal>
  → allowlist + moderator gate; goal ≥ 3 chars and ≤ MISSION_MAX_GOAL_CHARS
  → planner: fixed skill sequence for the mode, filtered by registered skills,
    capped at MISSION_MAX_STEPS (community: pulse → insights → kb.search;
    research: research.web → kb.search; draft_prep: kb.search → pulse [+ research.web])
  → persist mission + steps (idempotency key unique per guild) → run one step at a time
  → each step: orchestrator auth/limits → output validated → step persisted with references
  → live progress edits (bounded) + audit trace + truthful terminal status
moderator /mission-cancel → cooperative cancel flag; step boundary stops the run
startup → non-terminal missions become failed/interrupted, unfinished steps cancelled
```

Failure stops the mission at the failed step (remaining steps marked cancelled); the
summary reports honest completed/total counts and a terminal status. `/mission-view` and
`/mission-control` read guild-scoped rows only; no mission step writes, publishes, schedules
or executes anything outside the read-only skills.

## Web research (program Phase 3.3)

```
moderator /research <query>
  → enabled + provider + key configured? no → persisted `unavailable` report, no search
  → provider.search (Brave/Tavily; timeout; ≤ WEB_SEARCH_MAX_RESULTS; no page fetch)
  → validate/dedupe results; safe_source_url rejects non-public or credential URLs
  → bounded synthesis prompt over provider titles/snippets (untrusted, fenced)
  → strip URLs from model output → citations only from provider results
  → synthesis failure → extractive report from provider results
  → persist report + audit (no query text in audit) → render ≤ RESEARCH_MAX_SOURCES links
moderator /research-draft <report id> <channel>
  → completed + guild-owned report → ContentStudio.create_draft(source_kind='research')
  → audit research.draft_created; never submit/approve/publish
```

## Problem Radar (program Phase 3.4)

```
moderator /radar
  → Community Pulse minimum-data gate (insufficient → explicit state, no invented data)
  → guild-scoped summaries/KB rows only
  → observations: recurring questions, recurring error signatures, open threads, KB gaps
  → confidence low/medium/high from evidence counts + explicit uncertainty
  → recommended human action per item; rendered as evidence, never as a verdict
```

## Offline agent evaluation (program Phase 3.5)

```
configured dev /evaluate
  → temporary SQLite database only (never the runtime DB)
  → registry contract, invalid input, unknown skill, permission,
    guild/channel scope, redaction, image validation, migration,
    approval binding, database isolation checks
  → role roster, bounded dependency-aware planning, provider-availability honesty,
    agent-run dependency/idempotency/isolation
  → scheduler kinds (no publishing), idempotency, restart recovery, cancellation, guild scope
  → any failed check fails the report; report persisted guild-scoped + audit
  → explicit flags: offline_only=True, model_quality_evaluated=False
/evaluation-latest → last stored report for the guild
```

No Discord, Ollama or search-provider call is made by evaluation. A passing report means
the contracts and isolation behaved, not that model answers are good.

## Content planning (program Phase 3.6)

```
/calendar-add → guild-scoped informational row (kind, start, optional links to a draft or plan)
/calendar-schedule → link the entry to a durable job (read-only agent run or approval-gated draft)
                     only; the entry itself still schedules nothing
/calendar-cancel → conditional planned → cancelled; also cancels a linked job
/event-plan → deterministic template JSON from title/brief/schedule (creates nothing in Discord)
/event-plan-archive → conditional draft → archived
/reply-draft → one bounded model call over a pasted excerpt; mentions neutralized;
               persisted as a draft, never sent
/reply-dismiss → conditional draft → dismissed
```

## Host agent flow (gap closure)

```
moderator /agent <goal>
  → allowlist + moderator gate; goal cleaned, control tokens stripped, length-bounded
  → HostPlanner: classify goal → mode plan over registered read-only skills only
    (community: pulse → insights → radar → kb.search;
     research: kb.search [+ research.web];
     draft_prep: kb.search → pulse → content.brief [+ research.web];
     moderation: moderation.assess → knowledge candidates → insights;
     triage: kb.search → triage.analyze;
     evaluation: quality.checks)
  → validate plan: unique increasing indices, backward-only dependencies, read-only skills
  → persist run + steps (idempotency key unique per guild) → audit
  → executor: ready steps (all dependencies succeeded) run under AGENT_MAX_PARALLEL;
    each step goes through SkillOrchestrator scope/permission/limits checks
  → dependent steps get upstream outputs via the $upstream input key
  → final synthesis step combines results, notes conflicts and unavailable specialists
  → status completed / partial / failed / cancelled; every step keeps its own error
/agent-cancel → cancel flag + event; terminal runs are never restarted
startup → non-terminal agent runs close as failed/interrupted
```

Provider availability is reported, never simulated: if `research.web` is not registered, the
plan says the Web Researcher is unavailable instead of inventing sources. No step publishes,
approves, deletes, warns, times out or bans. The moderation specialist is classification-only,
the content writer returns a draft suggestion that is not saved as a Content Studio
draft, and the quality specialist shares the same offline checks as `/evaluate`
without persisting a report. Screenshot interpretation
stays in the mention-chat multimodal path (vision capability verified) while host triage
analyzes pasted text/logs deterministically.

## Attachment flow (gap closure)

```
message with attachments → count/size caps → per-file magic-byte format detection
  → image: decode → pixel/decompression guard → Pillow re-encode (metadata stripped)
  → PDF/DOCX/text/log/code: bounded extraction (pages/chars), unsupported → explicit notice
  → extracted text is fenced as untrusted data and included only in the current prompt
  → bounded metadata/extracted text persisted per guild/conversation for retention, then swept
follow-up message → recent turns may reference earlier attachments (ATTACHMENT_FOLLOWUP_TURNS)
```

The model answers distinguish observed evidence, inferred causes and open uncertainty, and may
suggest safe verification steps. It never executes user code or commands and never claims an
attachment was run.

## AI moderation flow (gap closure)

```
message event that explicitly mentions the bot (Message Content intent off) → moderation enabled?
  → guild policy enabled? → classification call (context, bounded)
  → category + severity + confidence vs policy thresholds
  → high-confidence + policy-enabled deletion + bot permission → delete → incident + warning
  → ambiguous/low-confidence → flag only, no deletion
  → private moderator-channel report (message link, category, confidence, severity, action,
    repeat count, recommended next step)
  → moderator review/override via modal; every transition audited without message text
retries → idempotent: no duplicate deletion, warning or report
```

No auto-ban, kick or timeout exists. Identity is never treated as evidence of guilt, and every
lookup is guild-scoped. Because the privileged Message Content intent stays off, the listener
assesses only messages that mention the bot; ordinary channel messages are out of scope and the
user-facing documentation says so.

## Content voice and approval coverage (gap closure)

```
/content-create|edit → guild voice profile + approved history + recent moderator feedback
  → exact content hash + near-duplicate (word-shingle Jaccard) check over recent approved work
  → history/feedback are untrusted data, fenced separately from system instructions
  → generated draft still goes through submit → different-human approval → publish gate
chat reply path → REPLY_APPROVAL_ENABLED=true → store exact text+channel approval, do not send
  → different human moderator approves → exact bound reply may be delivered
```

Editing a draft creates a new immutable version and invalidates any earlier approval. There is no
bypass flag and no model-visible switch.

## Durable scheduler flow (gap closure)

```
/schedule-agent|digest|draft|calendar-schedule
  → guild scope + moderator/approval-propose gate; future UTC-normalized run_at
  → idempotency key unique per guild (repeat returns the existing job)
  → persist job row (kind, payload, timezone, attempts, max_attempts) + audit
scheduler tick → due pending jobs → atomic claim (conditional pending→running)
  → dispatch: only agent.run (read-only), digest.collect (read-only), content.draft (draft only)
  → guild allowlist rechecked; timeout bounded
  → success → conditional succeeded transition + audit (result reference only)
  → failure → backoff retry up to SCHEDULER_MAX_ATTEMPTS → failed (permanent errors skip retry)
restart → running jobs return to pending and run again
/calendar-cancel → cancels the linked job; /schedule-cancel → cancels a pending or running job
cancellation → row becomes cancelled, the in-flight task is cancelled, and a racing
  success/failure transition or audit can never overwrite it
```

There is no publishing, sending, event-creation or moderation job kind. Draft jobs still require
explicit human approval and `CONTENT_PUBLISHING_ENABLED=true` before anything is sent.

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
guild/channel/user; `/conversation-status` shows that scope's turn count and timestamp bounds
(never text, with the same guild/channel checks); `/clear-conversation` deletes the caller's own
turns in one channel. Reads always prefer the database, so retention sweeps and clears can never
be resurrected by the in-memory fallback; a database failure degrades chat to in-process context
only (logged), and the status view states when only fallback memory exists. A reply to the bot
that Discord does not expose as readable text gets a rate-limited "mention me" hint instead of a
guess.

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
- Timeouts: per fetch (`FETCH_TIMEOUT_S`), per model call (`OLLAMA_REQUEST_TIMEOUT_S`), per job
  (`JOB_TIMEOUT_S`), per mission step (`MISSION_STEP_TIMEOUT_S`), per web search
  (`WEB_SEARCH_TIMEOUT_S`).

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
- Additional search providers behind the `research/provider.py` search-only interface (same URL
  validation and citation rules), and new mission modes as fixed plans over registered skills —
  never model-selected tool loops.
