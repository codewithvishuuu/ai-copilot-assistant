# Community Copilot — Configuration Reference

All configuration lives in a local `.env` file (never committed) read through
`src/copilot/config.py` (pydantic-settings). Every variable has a safe default except
`DISCORD_TOKEN`. Start from `.env.example`.

## Discord

| Variable | Default | Meaning / notes |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token from the Developer Portal. Loaded from env only; never logged; stored as a `SecretStr`. |
| `GUILD_ALLOWLIST` | *(empty)* | Comma-separated guild IDs allowed to use the bot. **Empty = bot refuses all guilds** (safe default). |
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

## Storage, privacy, logging

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `data` | Local data directory (gitignored). Relative to project root; portable. |
| `DB_FILENAME` | `copilot.db` | SQLite filename inside `DATA_DIR`. |
| `RETENTION_DAYS` | `90` | Stored messages/summaries older than this are swept. |
| `LOG_LEVEL` | `INFO` | Standard logging level for local logs. |
| `DEBUG_LOG_CONTENT` | `false` | If true, logs excerpts of message content — privacy risk, local debugging only. |
| `EPHEMERAL_BY_DEFAULT` | `true` | Slash replies only the requester sees (where Discord allows). Does not make source data private from the bot — see SECURITY.md. |

## Insights

| Variable | Default | Meaning |
|---|---|---|
| `INSIGHTS_MIN_THREADS` | `5` | Minimum stored summaries before `/insights` shows anything. |
| `INSIGHTS_WINDOW_DAYS` | `30` | Look-back window for insight aggregates. |

## Rules of thumb

- **Empty allowlists deny everything** — this is deliberate; fill them before inviting the bot.
- Prefer lowering `OLLAMA_NUM_CTX` and `MAX_CHARS` before lowering model quality on slow machines.
- Never commit `.env`. On a second machine, copy `.env.example` and re-enter values by hand.
