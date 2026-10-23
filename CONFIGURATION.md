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
| `LOG_LEVEL` | `INFO` | Standard logging level for local logs. |
| `DEBUG_LOG_CONTENT` | `false` | If true, logs excerpts of message content — privacy risk, local debugging only. |
| `EPHEMERAL_BY_DEFAULT` | `true` | Controls **`/summarize` replies only**: `true` = requester-only, `false` = channel-visible (for presentations). All other commands are always private regardless. Hiding the reply does not make source data private from the bot — see SECURITY.md. |

## Insights

| Variable | Default | Meaning |
|---|---|---|
| `INSIGHTS_MIN_THREADS` | `5` | Minimum number of distinct threads summarized within the look-back window (`INSIGHTS_WINDOW_DAYS`) before `/insights` or `/pulse` shows anything. Below it both commands say there is not enough data. |
| `INSIGHTS_WINDOW_DAYS` | `30` | Look-back window for insight aggregates and Community Pulse evidence. |

## Rules of thumb

- **Empty allowlists deny everything** — this is deliberate; fill them before inviting the bot.
- Multiple allowlisted guilds share one local database but not their data: KB rows, insights, and solution decisions are filtered by the interaction guild; target threads must match that guild and the channel allowlist.
- `/kb-search` shows approved solutions and auto-indexed thread summaries; only approved solutions can enter chat grounding.
- Prefer lowering `OLLAMA_NUM_CTX` and `MAX_CHARS` before lowering model quality on slow machines.
- Never commit `.env`. On a second machine, copy `.env.example` and re-enter values by hand.
