# Community Copilot

A **local-first AI assistant for technical Discord communities**. It summarizes long forum threads, classifies issue status, extracts unanswered questions and key findings, flags possibly-related older discussions, helps analyze errors and stack traces, and keeps a moderator-approved solution knowledge base — running entirely on a local model through [Ollama](https://ollama.com).

**Discord is the interface.** Slash commands and embeds are the entire UI: no website, no dashboard, no optional cloud AI API.

> **Project status: under active construction.** [PLAN.md](PLAN.md) is the single source of truth for what is implemented, tested, and verified — never trust this README over `PLAN.md`.

## Why local-first

- No cloud AI dependency: inference runs on your own machine via Ollama.
- Community data stays in a local SQLite database you control, with retention limits and deletion commands.
- Once the model is downloaded, **inference works offline** — but Discord itself always requires internet, and the bot process must be running during a demo.

## Requirements

| Item | Minimum (measured on this project's dev PC) |
|---|---|
| OS | Windows 10/11 (primary); Linux/macOS should work with minor path care |
| Python | 3.11+ |
| Ollama | recent version with `gemma4:e2b` pulled (~4.3 GB on disk) |
| RAM | 8 GB is the tight floor for `gemma4:e2b` — real numbers in [PLAN.md](PLAN.md) |
| Discord | one bot application + a test server (see setup below) |

## Quickstart (Windows)

```powershell
# 1. Environment
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Configuration (never commit .env)
Copy-Item .env.example .env
#    → fill in DISCORD_TOKEN and GUILD_ALLOWLIST (see CONFIGURATION.md)

# 3. Model (skip if already pulled)
ollama pull gemma4:e2b

# 4. Verify everything before running
.venv\Scripts\python.exe scripts\check_prereqs.py

# 5. Run the bot
.venv\Scripts\python.exe main.py
```

## Discord setup (Developer Portal)

1. Create an application → **Bot** → copy the token into `.env` as `DISCORD_TOKEN`.
   **Never** paste the token into chat, source code, or documentation.
2. Invite URL scopes: `bot` + `applications.commands` (the scope that enables slash
   commands — it is not a permission checkbox), with **only** these bot permissions:
   View Channels · Read Message History · Send Messages · Embed Links
   (permission integer `84992`). No Administrator, no Manage Server, no Kick/Ban,
   no Mention Everyone.
3. **No privileged intents are required.** The bot never listens to ordinary message events;
   it reads threads on demand only when someone runs `/summarize`.
4. For fast command sync while developing, set `TEST_GUILD_ID` in `.env`.

## Commands

Status column mirrors [PLAN.md](PLAN.md). `implemented` = code + mocked tests pass;
`verified` = also exercised against real Discord/Ollama. Automated tests pass, and selected
live Discord smoke tests have passed — not every command is live-tested yet
(see [DEPLOYMENT.md](DEPLOYMENT.md)).

| Command | Who | Status | Purpose |
|---|---|---|---|
| `/status` | everyone | implemented | Truthful health: Discord, Ollama, model, database, queue |
| `/summarize` | everyone | implemented | Summarize a forum thread: overview, problem, findings, decisions, open questions, status, next step, source links |
| `/cancel` | requester | implemented | Cancel your queued/running summarization |
| `/kb-search` | everyone | implemented | Search approved solutions + thread fingerprints (SQLite FTS5) |
| `/resolve` | moderator | implemented | Propose a resolution for a thread (needs approval to become official) |
| `/approve-solution` | moderator | implemented | Promote a proposed solution into the searchable knowledge base |
| `/reject-solution` | moderator | implemented | Reject a proposal (stays stored, not searchable) |
| `/correct-status` | moderator | implemented | Override the AI's issue-status classification |
| `/insights` | moderator | implemented | Recurring issues / unanswered-thread trends (minimum-data gated) |
| `/debug` | configured devs | implemented | Explain an error or stack trace (never executes anything) |
| `/forget` | moderator | implemented | Delete all stored data for a thread |

## Documentation map

| File | Contents |
|---|---|
| [PLAN.md](PLAN.md) | Phase status, decisions, tests, blockers — start here |
| [PRD.md](PRD.md) | Feature list with acceptance criteria |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module map, data flow, design invariants |
| [SECURITY.md](SECURITY.md) | Controls (each backed by code + test) |
| [THREAT_MODEL.md](THREAT_MODEL.md) | Adversaries, assets, mitigations |
| [AI_SYSTEM.md](AI_SYSTEM.md) | Model handling, prompts, output validation |
| [RAG.md](RAG.md) | Knowledge base + FTS5 retrieval design |
| [CONFIGURATION.md](CONFIGURATION.md) | Every environment variable |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Dev workflow, tests, lint |
| [TESTING.md](TESTING.md) | Test strategy + how to run |
| [DEMO_GUIDE.md](DEMO_GUIDE.md) | College presentation runbook |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Fresh-machine / second-laptop guide + troubleshooting |
| [CHANGELOG.md](CHANGELOG.md) | What changed, per phase |

## Privacy in one paragraph

Summaries are ephemeral (only the requester sees the reply) when Discord allows it. But an
ephemeral reply does **not** make the source messages private from the bot: it reads the
thread, may store a bounded excerpt locally (SQLite) for features you asked for, and keeps
it only within the configured retention window. Moderators can delete a thread's data with
`/forget`. Details and limits: [SECURITY.md](SECURITY.md).
