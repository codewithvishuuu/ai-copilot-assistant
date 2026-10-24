# Community Copilot — Configuration Reference

All configuration lives in a local `.env` file (never committed) read through
`src/copilot/config.py` (pydantic-settings). Every variable has a safe default except
`DISCORD_TOKEN`. Start from `.env.example`.

## Discord

| Variable | Default | Meaning / notes |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token from the Developer Portal. Loaded from env only; never logged; stored as a `SecretStr`. |
| `GUILD_ALLOWLIST` | *(empty)* | Comma-separated guild IDs allowed to use the bot. Multiple guilds are supported, but knowledge-base search/grounding, solution decisions, `/forget`, and insights are isolated per guild. **Empty = bot refuses all guilds** (safe default). |
| `CHANNEL_ALLOWLIST` | *(empty)* | Comma-separated channel/forum/thread IDs. Empty = all channels in allowed guilds. |
| `TEST_GUILD_ID` | *(empty)* | If set, slash commands are synced to this guild instantly (recommended in development). |
| `MODERATOR_ROLE_IDS` | *(empty)* | Extra role IDs treated as moderators in addition to Discord's `Manage Threads`/`Manage Messages` permissions. |
| `DEV_USER_IDS` | *(empty)* | User IDs allowed to run `/debug`. Empty = nobody. |

## Ollama (local model)

| Variable | Default | Meaning / notes |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server. Works unchanged on another machine. |
| `OLLAMA_MODEL` | `gemma4:e2b` | Any pulled Ollama tag. Capabilities (vision/context) are verified at runtime, not assumed. |
| `OLLAMA_NUM_CTX` | `4096` | Context window. **RAM-sensitive**: KV cache grows with this. Keep 2048–4096 on 8 GB machines. |
| `OLLAMA_REQUEST_TIMEOUT_S` | `180` | Timeout for one model call. |
| `OLLAMA_KEEP_ALIVE` | `5m` | How long the model stays loaded. Use short values on low-RAM machines; raise it for demo days so the ~34 s cold load is paid once. |
| `OLLAMA_MAX_TOKENS` | `1024` | Hard cap on generated tokens per model call (sent as `num_predict`). Bounds CPU time per request. |
| `OLLAMA_MAX_CONCURRENT` | `2` | Hard cap on simultaneous Ollama generation calls for the whole bot (1–8). Default `2` lets one summary job and one chat reply overlap, as before; `1` serializes everything (chat then waits behind a running job, bounded by `JOB_TIMEOUT_S`). Short health/capability probes are not counted. |
| `MODEL_TEMPERATURE` | `0.3` | Sampling temperature. Lower = more deterministic extraction. |
| `THINKING_ENABLED` | `false` | Gemma 4 thinking mode. Off = faster, more predictable JSON on CPU. |
| `VISION_ENABLED` | `true` | Master switch for image analysis; still requires the model to pass a runtime vision check. |

## Collection limits

| Variable | Default | Meaning |
|---|---|---|
| `MAX_MESSAGES` | `150` | Recent messages collected per `/summarize` (plus the opening post). |
| `HARD_MAX_MESSAGES` | `500` | Absolute ceiling; a per-command `count` option can never exceed this. |
| `MAX_CHARS` | `24000` | Total character budget sent to the model (oldest messages trimmed first, noted in the embed). |
| `MAX_IMAGES` | `4` | Maximum images considered per thread. |
| `MAX_IMAGE_BYTES` | `6000000` | Per-image byte cap (~6 MB). |

## Queue and rate limits

| Variable | Default | Meaning |
|---|---|---|
| `QUEUE_CAPACITY` | `5` | Pending jobs before new requests are refused with "busy". |
| `PER_USER_COOLDOWN_S` | `60` | Seconds between two accepted requests from one user. |
| `PER_GUILD_HOURLY_LIMIT` | `20` | Max accepted requests per guild per hour. |
| `JOB_TIMEOUT_S` | `300` | Overall cap for one summarization job (covers queue wait + work). |
| `FETCH_TIMEOUT_S` | `60` | Timeout for Discord history fetching. |

## Conversation (mention chat)

| Variable | Default | Meaning |
|---|---|---|
| `CONVERSATION_ENABLED` | `true` | Master switch for @mention chat. Slash commands are unaffected. |
| `CONVERSATION_MAX_TURNS` | `6` | Exchanges loaded into one chat prompt per `(guild, channel, user)`. Older stored turns stay stored (until retention) but are not prompted. |
| `CONVERSATION_MAX_CHARS` | `6000` | Character budget for the chat prompt (persona + history + question); oldest history is dropped first. |
| `CONVERSATION_TTL_S` | `3600` | Seconds of inactivity after which the in-process fallback context expires (the SQLite history is the source of truth while the database is available). |
| `CONVERSATION_COOLDOWN_S` | `10` | Minimum seconds between two model-backed replies to the same user. |
| `CONVERSATION_TIMEOUT_S` | `120` | Timeout for one chat model call (separate from `OLLAMA_REQUEST_TIMEOUT_S`). |
| `CONVERSATION_MAX_TOKENS` | `512` | Token cap for one chat reply (sent as `num_predict`). |
| `CONVERSATION_RETENTION_DAYS` | `7` | Stored chat turns older than this are deleted by the daily retention sweep (1–90). |
| `CONVERSATION_MAX_STORED_TURNS` | `40` | Hard cap on stored turns per `(guild, channel, user)`; the oldest are pruned in the same transaction as every write (must be ≥ 2 × `CONVERSATION_MAX_TURNS`). |
| `CONVERSATION_MAX_WAITING` | `2` | Chat requests allowed to WAIT for the single in-flight chat generation (0–10). Beyond this a requester gets at most one rate-limited "busy" notice (once per 5 minutes per user) instead of piling up waiting tasks. `0` = when busy, answer immediately without waiting. |
| `CONVERSATION_GROUNDING_ENABLED` | `true` | Master switch for the optional KB-grounded chat block (Phase 4, D22 — implemented; live smoke step 19 pending). Only guild-scoped, moderator-approved `kb_docs` (`approved=1`) may enter it; auto-indexed thread fingerprints are excluded. Retrieval failures fail open to plain chat and are never user-visible. |
| `CONVERSATION_GROUNDING_TOP_K` | `2` | Maximum knowledge-base documents considered per chat prompt (1–5). At most two are shown as clearly labeled "possibly related (not verified)" sources. |
| `CONVERSATION_GROUNDING_MAX_CHARS` | `1200` | Character cap for the whole KB block, fence and headers included (200–4000). When including the block would exceed `CONVERSATION_MAX_CHARS`, the **whole block is dropped before any history is removed** — conversation continuity outranks retrieval. |

Cooldown, cooldown-window requests and at-capacity requests are not silently dropped: the user gets
at most one short, model-free notice per 5 minutes (`CONVERSATION_COOLDOWN_S` still governs when a
model-backed reply is allowed). A message that arrives while the *same scope's* reply is still being
generated is skipped silently — the typing indicator already shows that work is in progress.

Chat turns are stored in the local SQLite database (`conversation_turns`, migration 3), scoped
strictly per guild/channel/user, so context survives restarts. Stored history is bounded by age
(`CONVERSATION_RETENTION_DAYS`) and by the per-scope row cap (`CONVERSATION_MAX_STORED_TURNS`);
`/clear-conversation` deletes your own turns in a channel immediately. If the database is
unavailable, chat degrades to the in-process context only (bounded by `CONVERSATION_MAX_TURNS`,
`CONVERSATION_MAX_CHARS`, `CONVERSATION_TTL_S`) and says so in the logs.

## Missions (program Phase 3.2)

| Variable | Default | Meaning |
|---|---|---|
| `MISSIONS_ENABLED` | `true` | Enables moderator `/mission*` and Mission Control. Missions are read-only plans over registered skills; disabling hides the commands without touching stored mission rows. |
| `MISSION_MAX_STEPS` | `4` | Hard cap on steps per mission (1-6). Mode plans are shorter than the cap; extra planned steps are dropped, never invented. |
| `MISSION_MAX_GOAL_CHARS` | `500` | Maximum goal length accepted by `/mission` (20-1000). Longer goals are rejected before anything is stored. |
| `MISSION_STEP_TIMEOUT_S` | `45` | Timeout for one mission step. A timeout fails that step and stops the mission honestly. |
| `MISSION_MAX_RESULT_CHARS` | `8000` | Per-step result cap used for persistence and rendering (500-20000). |

Missions never publish, schedule or execute external actions; they persist guild-scoped
missions/steps with a trace id, support cooperative cancellation between steps, and close
interrupted runs as `failed`/`interrupted` at startup.

## Web research (program Phase 3.3)

| Variable | Default | Meaning |
|---|---|---|
| `WEB_RESEARCH_ENABLED` | `false` | Master switch. When false, `/research` records/reports `unavailable` and never calls a provider. |
| `WEB_SEARCH_PROVIDER` | `none` | `none`, `brave`, or `tavily`. Any other value fails startup validation. |
| `BRAVE_SEARCH_API_KEY` | empty | Key used only when `WEB_SEARCH_PROVIDER=brave`. Stored as a secret and redacted from logs. |
| `TAVILY_API_KEY` | empty | Key used only when `WEB_SEARCH_PROVIDER=tavily`. |
| `WEB_SEARCH_TIMEOUT_S` | `20` | Timeout for one search provider call. |
| `WEB_SEARCH_MAX_RESULTS` | `5` | Results requested/accepted per query (1-8). |
| `WEB_SEARCH_MAX_CHARS` | `6000` | Total character budget for titles/snippets passed to synthesis (500-20000). |
| `RESEARCH_MAX_SOURCES` | `5` | Maximum cited sources shown in a report (1-8). |
| `RESEARCH_SYNTHESIS_MAX_TOKENS` | `600` | Token cap for the model synthesis call (64-2000). Synthesis failure falls back to an extractive report. |

Search is the only network call to the provider: page contents are never fetched. URLs are
validated as public `http(s)` without credentials; model output is stripped of URLs so citations
come only from provider results. The research query is stored locally in `research_reports`;
the audit row never includes the query text.

## Content planning (program Phase 3.6)

| Variable | Default | Meaning |
|---|---|---|
| `CONTENT_CALENDAR_MAX_ENTRIES` | `100` | Maximum informational calendar rows returned per guild listing (1-500). Nothing is scheduled in Discord. |
| `CONTEXTUAL_REPLY_MAX_CHARS` | `800` | Maximum length of a generated reply draft (100-1800). Drafts are never sent automatically. |
| `EVENT_PLAN_MAX_CHARS` | `1500` | Maximum rendered event-plan template size (100-4000). Plans create nothing in Discord. |

Calendar entries alone schedule nothing. `/calendar-schedule` can link an entry to a durable
job, and that job may only run read-only analysis (`agent.run`/`digest.collect`) or create an
approval-gated Content Studio draft (`content.draft`). Cancelling the entry cancels its linked
job. There is no scheduled-publishing job kind.

## Main Host Agent (gap closure)

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_ENABLED` | `true` | Enables `/agent*`. Disabling refuses new runs; stored runs remain readable. |
| `AGENT_MAX_STEPS` | `5` | Plan cap (1-6). One slot is reserved for the final synthesis step. |
| `AGENT_MAX_GOAL_CHARS` | `500` | Maximum goal length (20-800). Longer input is truncated before planning. |
| `AGENT_MAX_PARALLEL` | `2` | Maximum independent steps executed concurrently (1-4). Dependencies always wait. |
| `AGENT_RUN_TIMEOUT_S` | `150` | Whole-run timeout. Unfinished work ends as `failed`/`cancelled`, never as success. |

The host selects only registered read-only skills, passes upstream outputs to dependents,
combines results, and reports conflicts, unavailable specialists and partial failures. It never
deletes, warns, times out, bans, publishes or approves.

## Attachment ingestion (gap closure)

| Variable | Default | Meaning |
|---|---|---|
| `ATTACHMENTS_ENABLED` | `true` | Enables attachment handling in mention chat. |
| `ATTACHMENT_MAX_COUNT` | `5` | Attachments per message (1-10). |
| `ATTACHMENT_MAX_BYTES` | `8000000` | Per-file byte cap (100 KB - 25 MB). |
| `ATTACHMENT_MAX_TEXT_CHARS` | `16000` | Extracted-text cap per attachment (500-60000). |
| `ATTACHMENT_MAX_PDF_PAGES` | `30` | PDF page cap (1-100). |
| `ATTACHMENT_MAX_IMAGE_PIXELS` | `20000000` | Decoded pixel cap before re-encode (decompression-bomb guard). |
| `ATTACHMENT_RETENTION_DAYS` | `3` | Retention for attachment metadata/extracted text (1-30). |
| `ATTACHMENT_FOLLOWUP_TURNS` | `6` | Recent turns that may refer back to earlier attachments. |

Magic bytes — not the filename or Discord MIME hint — decide the format. Images are validated
and re-encoded; PDFs/DOCX are extracted with page/character bounds; unsupported formats get an
explicit response. Extracted text is untrusted, fenced, never executed, and only bounded text
metadata is stored (not the whole file).

## AI moderation (gap closure)

| Variable | Default | Meaning |
|---|---|---|
| `MODERATION_ENABLED` | `false` | Master switch. Each guild still needs an enabled policy row. |
| `MODERATION_REPEAT_WINDOW_DAYS` | `7` | Window used to count confirmed repeat incidents. |
| `MODERATION_TIMEOUT_S` | `30` | Timeout for one classification call. |
| `MODERATION_MAX_TOKENS` | `300` | Token cap for classification output. |
| `MODERATION_DEFAULT_CONFIDENCE` | `0.75` | Default for `/moderation-policy` when its `confidence` option is omitted (0-1). |
| `MODERATION_DEFAULT_SEVERITY` | `high` | Default minimum severity for automatic action. |

The report channel is configured per guild with `/moderation-policy`; there is no global channel
setting.

**Scope: mentions only.** The privileged Message Content intent stays off, so Discord delivers
message text only when the bot is mentioned. The listener enforces that scope explicitly: it
assesses only messages that mention the bot, and ordinary channel messages are not moderated.
Enabling the privileged intent is a separate deployment decision and is not supported here.

Classification evaluates context, not isolated keywords. Ambiguous or low-confidence cases are
flagged for human review. Deletion happens only when the per-guild policy enables it and the bot
has the permission; otherwise the incident is reported without acting. There is no automatic
ban, timeout or kick. Retries are idempotent: a message is not deleted, warned or reported twice.

## Durable scheduler (gap closure)

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | Enables scheduling commands and the background tick loop. |
| `SCHEDULER_TICK_S` | `30` | Interval between due-job scans (5-300). |
| `SCHEDULER_TICK_BATCH` | `10` | Maximum jobs claimed per scan (1-50). |
| `SCHEDULER_JOB_TIMEOUT_S` | `120` | Timeout for one job; a timeout is retried with backoff. |
| `SCHEDULER_MAX_ATTEMPTS` | `3` | Attempts before a job is marked `failed` (1-10). |

Jobs are stored in SQLite, claimed atomically, retried with capped exponential backoff, and
returned to `pending` after a restart. Times are stored in UTC; IANA timezone names require the
`tzdata` package (absent on some Windows hosts), so use `UTC` or an ISO timestamp with an
explicit offset there. Job kinds are limited to `agent.run`, `digest.collect` and
`content.draft`; scheduled publishing does not exist, and `CONTENT_PUBLISHING_ENABLED` still
gates any later human-approved publication.

## Chat reply approval (gap closure)

| Variable | Default | Meaning |
|---|---|---|
| `REPLY_APPROVAL_ENABLED` | `false` | When true, generated chat replies are not sent until a different human moderator approves the exact text and channel. |
| `REPLY_APPROVAL_TTL_S` | `3600` | Approval lifetime (60 s - 7 days). Expired requests cannot be sent. |

## Content voice memory (gap closure)

| Variable | Default | Meaning |
|---|---|---|
| `CONTENT_FEEDBACK_LIMIT` | `6` | Recent moderator feedback notes reused in generation prompts (0-20). |
| `CONTENT_SIMILARITY_THRESHOLD` | `0.82` | Word-shingle Jaccard threshold for near-duplicate detection (0.5-0.99). |
| `CONTENT_DEDUPE_RECENT` | `40` | Recent approved drafts/posts compared for repetition (5-200). |

Approved history and feedback are untrusted data, separated from system instructions. Exact
content hashes are always checked; near-duplicate warnings never block a human decision.

There are no settings for `/evaluate`: it always runs offline against temporary databases and
never contacts Discord, Ollama or search providers.

## Content Studio (Phase 2B)

| Variable | Default | Meaning |
|---|---|---|
| `CONTENT_PUBLISHING_ENABLED` | `false` | Master switch for real Discord publication. When false, drafts can still be generated/reviewed and stuck claims can still be recovered, but `/content-publish` refuses before claiming. Enable only after the bot can post in the intended channel. |
| `CONTENT_GENERATION_MAX_CHARS` | `1800` | Hard cap for generated/edited draft text (100-2000, Discord's single-message limit). Longer output is rejected, not truncated into a publishable draft. |
| `CONTENT_GENERATION_TIMEOUT_S` | `120` | Timeout for one draft-generation model call. |
| `CONTENT_GENERATION_MAX_TOKENS` | `1024` | Token cap for draft generation (64-4096). |
| `CONTENT_APPROVAL_TTL_S` | `3600` | Lifetime of a content approval request (60 s - 7 days). Approval is rechecked at publication; expired requests cannot be sent. |
| `CONTENT_CONTEXT_MAX_CHARS` | `2000` | Total character budget for optional approved-KB context in a generation prompt (0-8000; 0 disables retrieval). |
| `CONTENT_CONTEXT_TOP_K` | `3` | Maximum approved KB documents used as generation context (0-5). |

The target channel must belong to the interaction guild and pass `CHANNEL_ALLOWLIST` when that
list is non-empty; the bot needs View Channel, Read Message History, Send Messages and Embed Links
there. Drafts, versions and audit rows are retained locally. Publishing is at-most-once; after a
timeout/ambiguous send, use `/content-recover` while the target channel history still contains the
post (recovery scans the latest 100 messages for the deterministic nonce). See
[ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](SECURITY.md).

## Storage, privacy, logging

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `data` | Local data directory (gitignored). Relative to project root; portable. |
| `DB_FILENAME` | `copilot.db` | SQLite filename inside `DATA_DIR`. |
| `RETENTION_DAYS` | `90` | Stored messages/summaries older than this are swept. |
| `LOG_LEVEL` | `INFO` | Standard logging level for local logs. Message bodies are never logged. |
| `EPHEMERAL_BY_DEFAULT` | `true` | Controls **`/summarize` replies only**: `true` = requester-only, `false` = channel-visible (for presentations). All other commands are always private regardless. Hiding the reply does not make source data private from the bot — see SECURITY.md. |

## Insights

| Variable | Default | Meaning |
|---|---|---|
| `INSIGHTS_MIN_THREADS` | `5` | Minimum number of distinct threads summarized within the look-back window (`INSIGHTS_WINDOW_DAYS`) before `/insights` or `/pulse` shows anything. Below it both commands say there is not enough data. |
| `INSIGHTS_WINDOW_DAYS` | `30` | Look-back window for insight aggregates and Community Pulse evidence. |

## Rules of thumb

- **Empty allowlists deny everything** — this is deliberate; fill them before inviting the bot.
- Multiple allowlisted guilds share one local database but not their data: KB rows, insights, solution decisions, missions, research reports, radar results, calendar/event plans, reply drafts, content drafts and evaluation reports are filtered by the interaction guild; target threads must match that guild and the channel allowlist.
- Leave `WEB_RESEARCH_ENABLED=false` unless you intend to send research queries to Brave/Tavily. A missing key is reported as `unavailable`, and no source is fabricated.
- Keep `CONTENT_PUBLISHING_ENABLED=false` until a human has reviewed the exact approved draft and the bot can post in the target channel; planning, mission, agent and scheduler commands cannot publish on their own.
- `/kb-search` and every user-facing retrieval path return approved documents only by default; candidate review uses the separately authorized moderator workflow (`approved_only=False`). A guild's documents never appear in another guild's results.
- Prefer lowering `OLLAMA_NUM_CTX` and `MAX_CHARS` before lowering model quality on slow machines.
- Never commit `.env`. On a second machine, copy `.env.example` and re-enter values by hand.
