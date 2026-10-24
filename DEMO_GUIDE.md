# Demo Guide — College Presentation Runbook

A 5–7 minute live demo of Community Copilot on a demo laptop. The whole point of the project
is that **everything runs locally** — no cloud account, no API key, no dashboard.

## Before the demo (T-30 minutes)

| # | Step | Why |
|---|---|---|
| 1 | Start Ollama headless if the tray app server is dead: `ollama serve` | The tray server can die (GPU watchdog timeouts observed on the dev PC) |
| 2 | In `.env`: `OLLAMA_KEEP_ALIVE=6h` | The model's cold load is ~34 s; pay it once before the audience arrives |
| 3 | Warm-up: run `/summarize` on any small thread yourself | Confirms the model is loaded and the bot answers in warm ~4–6 s |
| 4 | In `.env`: `EPHEMERAL_BY_DEFAULT=false` **for demo day only** | Makes only the **`/summarize`** reply public — the highlight of the show. Every other command stays private regardless of this setting (see the visibility note below). Flip it back after the demo |
| 5 | Run `.venv\Scripts\python.exe scripts\check_prereqs.py` | Every line `[ OK ]` except the intentional ones; catches a missing model before the show |
| 6 | Start the bot: `.venv\Scripts\python.exe main.py`; confirm command sync | Slash commands may not appear if `TEST_GUILD_ID` is unset (global sync is slow) |
| 7 | Prepare three threads in the test guild (below) | The demo needs the right material, not luck |

### Prepare these threads

- **Thread A — active troubleshooting** (~12–20 messages): a fake-but-realistic issue ("uploads
  fail after the upgrade"), a few users participating, one reply containing `@everyone`
  (to prove the inertness), one screenshot image (for the vision step).
- **Thread B — old, resolved**: a short thread you will promote to the knowledge base through
  `/resolve` → `/approve-solution`.
- **Thread C — error text**: paste a long Python stack trace into a thread for `/debug`.

## The script (≈6 minutes)

> **Visibility on stage:** `EPHEMERAL_BY_DEFAULT=false` makes only **`/summarize`** replies public
> to the channel. Every other command — `/cancel`, `/status`, `/correct-status`, `/resolve`,
> `/approve-solution`, `/reject-solution`, `/kb-search`, `/debug`, `/insights`, `/forget`,
> `/clear-conversation` — is **always private** (requester-only) no matter what. Present those
> from your own client with screen sharing and narrate what the audience cannot see.

| Time | Step | What you say / show |
|---|---|---|
| 0:00 | Intro | "Discord servers drown in repetitive threads. This bot lives inside Discord, runs a local model on this laptop, and no chat ever leaves the machine." |
| 0:30 | `/status` | Truthful health panel: Discord latency, Ollama version, model installed, DB schema, queue. "If Ollama is down, this says so — the bot never pretends." |
| 1:00 | `/summarize` on Thread A | Reply appears (warm: seconds). Show: overview, main problem, key findings **with clickable source links**, decisions, open questions, status + confidence, suggested next step, "AI suggestion — verify" labeling, the footer. |
| 2:30 | Point at the `@everyone` mention inside a finding | "Content is fenced as untrusted data, mentions are neutralized — nothing from a thread can ping you or instruct the model." |
| 3:00 | Screenshot step | Show that the image reply was included: "the summary notes that 1 image was used as model input — and images are re-encoded locally with metadata stripped first." |
| 3:30 | Moderator `/correct-status` | "Humans stay in charge: the AI suggests a status, a moderator can override it, and the correction is what future readers see." |
| 4:00 | `/resolve` Thread B → `/approve-solution` | "Solutions enter the knowledge base only after a moderator approves them." |
| 4:30 | `/kb-search` for Thread A's error | Approved solution appears with source link. "This is SQLite FTS5 — lexical, so it's labeled 'possibly related (not verified)'. No embeddings on an 8 GB laptop, and we'd rather say 'possibly' than hallucinate certainty." |
| 5:00 | `/debug` with Thread C's stack trace | Explanation with likely causes and safe next steps; the embed states nothing was executed or fetched. |
| 5:30 | `/insights` | Likely shows the **gate**: "Not enough summarized threads yet — the bot refuses to make trend claims from too little data. In a real community this unlocks as data accumulates." |
| 5:45 | Mention chat (optional, if time allows) | "@mention the bot — typing indicator, then a bounded answer that remembers your last message. Ask for a long code sample: the split keeps every code block valid. Send a rapid second question: one short 'give me a few seconds' line, never a duplicate answer." |
| 6:00 | Privacy close | "Everything is in one local SQLite file with a retention window; `/forget` deletes a thread's content, chat turns, moderation records, pending replies and attachments — including the search index — while audit and workflow history are retained. Mention-chat turns are stored scoped per user and channel, bounded by a 7-day default retention, and `/clear-conversation` deletes your own history in a channel instantly." |

## If something goes wrong on stage

- **Model still cold / very slow**: point at the queue message ("you are 1st in queue") and use
  the benchmark table in [AI_SYSTEM.md](AI_SYSTEM.md) — cold ~34 s, warm ~4–6 s, ~16–28 tok/s,
  measured on this exact machine. Honesty about latency is part of the story, not a failure.
- **Ollama died**: `/status` shows it red — narrate it as a feature, restart `ollama serve`,
  retry; or continue with `/kb-search` / `/status` which don't need the model.
- **Bot answers with one short "give me a few seconds" line**: that's the throttle notice —
  narrate it: "throttling is bounded and honest: at most one notice, never silence, never a
  duplicate answer."
- **Discord hiccups**: fall back to the pre-seeded knowledge base (`/kb-search`) and `/status`;
  they prove the platform without inference.
- **Never fake results.** If a step fails, say what failed — the project's whole credibility
  claim is "nothing is claimed until verified" (see [PLAN.md](PLAN.md)).

## Q&A cheat sheet

| Question | One-line answer |
|---|---|
| Why local instead of an API? | Privacy and cost: community content never leaves the machine, and there is no per-message bill. |
| Which model? | `gemma4:e2b` via Ollama (4.6 GB, Q4_K_M); vision is verified through `/api/show`, and the repo's Phase 0 measurements back every latency claim. |
| Why not embeddings / a vector DB? | On an 8 GB CPU laptop, FTS5 BM25 is fast, predictable, and debuggable; we label results "possibly related" and document the recall limitation in RAG.md. |
| Can the model run code or open links? | No. It has no tools, no shell, no filesystem, no network. Prompt text is fenced; control tokens are stripped; output is schema-validated and source IDs are checked against the real thread. |
| What about prompt injection in threads? | Treated as a first-class threat: fenced untrusted data, no tool access, output validation, mention neutralization — THREAT_MODEL.md lists the exact cases. |
| Does it need a server or dashboard? | No. Discord is the entire interface; deployment is one folder + Ollama (DEPLOYMENT.md). |
| What permissions does the bot need? | View Channels, Read Message History, Send Messages, Embed Links (permission integer `84992`), plus the `applications.commands` scope for slash commands — and **no privileged intents**. Mention chat needs no Message Content intent either: Discord only shows the bot a message's text when the bot is mentioned. |
| What happens to my data? | Local SQLite with `RETENTION_DAYS` sweep and `/forget`; mention-chat turns are scoped per user+channel with a 7-day default `CONVERSATION_RETENTION_DAYS` sweep and `/clear-conversation`; message bodies are never written to logs. |
| Does mention chat remember across restarts? | Yes — your turns persist in the local SQLite database, isolated per `(guild, channel, user)`, so a follow-up after a bot restart still has context. |
| What if several people mention the bot at once? | Chat generates one reply at a time; a bounded number of requests may wait, and anyone beyond that gets one short "busy" notice — no pile-up of typing indicators. A `/summarize` job still runs in parallel: the shared model cap defaults to 2 (one job + one chat). |
