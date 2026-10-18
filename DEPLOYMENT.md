# Deployment — Fresh Machine & Second Laptop

Community Copilot is **local-first**: there is no server to deploy to. "Deployment" means
getting it running on a fresh Windows machine (or the presentation laptop) without OpenCode
or any cloud account. Everything runs from one folder plus a local Ollama server.

## Prerequisites

| Item | Detail |
|---|---|
| Python | 3.11+ (`python --version`); the venv isolates everything |
| Ollama | Installed and running locally; `gemma4:e2b` pulled (~4.6 GB) |
| RAM | 8 GB is the tight floor; the model needs ~3–4 GB free while loaded |
| Disk | ~6 GB free for the model + small SQLite data directory |
| Discord | One bot application (token) + a test server where you can add it |

## Fresh install (Windows, from a copy of this repo)

```powershell
# 1. Copy the repo folder to the target machine (without .venv/ and data/ — see below)

# 2. Create the environment
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Local model
ollama pull gemma4:e2b

# 4. Configuration — copy, then edit with your own values
Copy-Item .env.example .env
#    Required: DISCORD_TOKEN, GUILD_ALLOWLIST (empty allowlist = bot refuses every guild)
#    Recommended while developing: TEST_GUILD_ID (commands sync instantly)

# 5. Verify the environment
.venv\Scripts\python.exe scripts\check_prereqs.py

# 6. Run
.venv\Scripts\python.exe main.py
```

### What to copy and what not to copy

| Item | Copy? | Why |
|---|---|---|
| `src/`, `tests/`, `scripts/`, `main.py`, docs, `requirements*.txt` | yes | the project |
| `.env` | only by hand, never in a zip/chat | contains your bot token; it is gitignored for a reason |
| `.venv/` | no | machine-specific; recreate it (step 2) |
| `data/` | optional | contains the SQLite DB + logs; delete for a clean install, or copy `copilot.db` to bring a pre-seeded demo knowledge base |

## Recorded environment check (2026-10-18, dev PC)

Real output of `scripts\check_prereqs.py` on the dev machine — the one `FAIL` is expected
until you create `.env`; the script exits non-zero so CI/scripts can detect it:

```text
Community Copilot — environment check
project: C:\Users\VISHAL KUMAR\community-copilot
--------------------------------------------------------------

[ OK ] Python           3.11.9
[ OK ] SQLite FTS5      sqlite 3.45.1
[ OK ] Ollama CLI       C:\Users\VISHAL KUMAR\AppData\Local\Programs\Ollama\ollama.EXE
[ OK ] Ollama server    v0.40.2 at http://localhost:11434
[ OK ] Model installed  gemma4:e2b — capabilities: completion, vision, audio, tools, thinking; context 131,072
[FAIL] .env file        missing — copy .env.example to .env and fill it in
[ OK ] Data directory   writable: C:\Users\VISHAL KUMAR\community-copilot\data
[ OK ] Disk space       95.3 GB free (model needs ~4.6 GB)
[ OK ] RAM              8.4 GB total, 3.2 GB free — expect CPU-limited generation on this machine
--------------------------------------------------------------
1 check(s) need attention before running the bot.
```

## Troubleshooting (observed on the dev machine)

| Symptom | Cause / fix |
|---|---|
| Ollama unreachable in `/status` | Server not running. The Windows tray app's server can die (GPU watchdog timeouts were observed); run headless: `ollama serve` in a terminal and retry. |
| First `/summarize` takes ~30–40 s before any output | Cold model load (~34 s measured). Warm replies are ~4–6 s. For demo days raise `OLLAMA_KEEP_ALIVE` (e.g. `6h`) so the load is paid once. |
| Summaries feel slow while streaming nothing | CPU generation is ~16–28 tokens/s; the queue shows a "queued" message with position. Prefer shorter threads; lower `MAX_CHARS` if needed. |
| Slash commands don't appear | Global sync can take up to an hour. Set `TEST_GUILD_ID` in `.env` for instant guild-scoped sync. |
| Bot ignores every command in your server | `GUILD_ALLOWLIST` is empty or doesn't include that guild — empty **denies all** by design (safe default). |
| Port 11434 already in use / custom Ollama host | Set `OLLAMA_BASE_URL` (and/or `OLLAMA_HOST` for the server) in `.env`. |
| Model missing after reinstall | `ollama pull gemma4:e2b` — check with `ollama list`; `/status` also reports this truthfully. |

## Manual smoke test (live Discord) — **not yet performed**

This checklist is what the `implemented (manual)` rows in [SECURITY.md](SECURITY.md) refer to.
It requires a real token in `.env` and a test guild. Run it once per new machine:

1. Start the bot: it logs startup, syncs commands, opens the DB — no token anywhere in logs.
2. `/status` — every section reflects reality (server/model/DB/queue); try it with Ollama
   stopped and confirm it says so instead of pretending.
3. `/summarize` on a small real thread — reply is ephemeral (default), carries the
   "AI-generated — verify before acting" footer and clickable source-message links.
4. Post a message containing `@everyone` inside a test thread, summarize it, confirm the
   embed shows it **inert** (no ping fired).
5. `/debug` as a non-allowed user — refused; as an allowed user — explanation appears.
6. As a moderator: `/resolve` a thread → `/approve-solution` → `/kb-search` finds it.
7. `/correct-status` on a summarized thread — the correction is reflected on a re-run.
8. `/forget` the test thread, then `/kb-search` — its data (including FTS rows) is gone.
9. Ctrl+C the bot mid-job — it shuts down gracefully (worker cancelled, DB closed).
10. Restart — previously stored summaries/KB survive (SQLite persistence).

## Uninstall / cleanup

```powershell
# Bot data (summaries, KB, logs)
Remove-Item -Recurse -Force data

# Model (reclaim ~4.6 GB)
ollama rm gemma4:e2b

# Environment
Remove-Item -Recurse -Force .venv
```
