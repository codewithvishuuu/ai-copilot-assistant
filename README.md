<<<<<<< HEAD
# Community Copilot

A **local-first AI assistant for technical Discord communities**. It summarizes long forum threads, classifies issue status, extracts unanswered questions and key findings, flags possibly-related older discussions, helps analyze errors and stack traces, runs bounded read-only multi-step missions with live progress, researches the web with cited sources, shows evidence-backed community health observations, evaluates its own safety and tool contracts offline, keeps a moderator-approved solution knowledge base, answers @mention questions in a bounded local chat that remembers recent context across restarts, and drafts community content, calendars, event plans and reply suggestions for human review, delegates bounded read-only work to specialist agent roles, ingests screenshots/logs/documents safely, runs durable analysis or draft jobs, and can flag configured abuse categories for human moderation — running entirely on a local model through [Ollama](https://ollama.com).

**Discord is the interface.** Slash commands, @mention chat, and embeds are the entire UI: no website, no dashboard, no optional cloud AI API.

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
3. **No privileged intents are required.** The bot uses only non-privileged message
   events (mention-triggered chat); the **Message Content intent stays off**, so Discord
   only exposes a message's text when the bot is mentioned. AI moderation is therefore
   **mention-only**: ordinary channel messages are not assessed. Threads are read on
   demand when someone runs `/summarize`.
4. For fast command sync while developing, set `TEST_GUILD_ID` in `.env`.

## Commands

Status column mirrors [PLAN.md](PLAN.md). `implemented` = code + mocked tests pass;
`verified` = also exercised against real Discord/Ollama. Automated tests pass, and selected
live Discord smoke tests have passed — not every command is live-tested yet
(see [DEPLOYMENT.md](DEPLOYMENT.md)).

Besides slash commands, you can **@mention the bot** with a question — it replies in
English, Hindi, or Hinglish with bounded context that persists across restarts, scoped
per user and channel (limits and retention in [CONFIGURATION.md](CONFIGURATION.md)).
Long answers are split at natural boundaries; a code fence that spans a cut is closed
and reopened with its language tag, so neither chunk renders broken. Simultaneous model
calls are capped by one shared limit, a bounded number of chat requests may wait, and
throttled or saturated requests get at most one short notice instead of silence.
Mention chat, follow-up context, the typing indicator, and basic image interpretation
are live-smoke-tested; the per-item truth lives in [PLAN.md](PLAN.md).

The internal program skill foundation is read-only and has no model tool surface. It
exposes approved knowledge search (`kb.search`), guild-scoped insights
(`insights.community`), Community Pulse (`pulse.community`, moderators), pending
candidate review info (`knowledge.candidates`, moderators), and configured web research
(`research.web`, moderators). Skill IDs are selected by trusted application code, never by
the model. Candidate creation, missions, drafts and publication remain human commands.

Missions are bounded, human-started plans over those read-only skills (max 4 steps by
default) with per-step persistence, live progress, cooperative cancellation, startup
recovery for interrupted runs, and an audit trace. Web research is **disabled unless an
operator configures a provider and key** (`WEB_RESEARCH_ENABLED`); it performs search-API
queries only (no page fetching), rejects private/local URLs, and cites only URLs returned
by the provider. When research is unavailable the bot reports that honestly instead of
inventing sources. `research-draft` transfers a completed report into Content Studio with
stored provenance — it never publishes.

| Command | Who | Status | Purpose |
|---|---|---|---|
| `/status` | everyone | implemented | Truthful health: Discord, Ollama, model, database, queue |
| `/summarize` | everyone | implemented | Summarize a forum thread: overview, problem, findings, decisions, open questions, status, next step, source links |
| `/cancel` | requester | implemented | Cancel your queued/running summarization |
| `/kb-search` | everyone | implemented | Search approved knowledge only (SQLite FTS5); candidate review is a separate moderator workflow |
| `/resolve` | moderator | implemented | Propose a resolution for a thread (needs approval to become official) |
| `/approve-solution` | moderator | implemented | Promote a proposed solution into the searchable knowledge base |
| `/reject-solution` | moderator | implemented | Reject a proposal (stays stored, not searchable) |
| `/correct-status` | moderator | implemented | Override the AI's issue-status classification |
| `/insights` | moderator | implemented | Recurring issues / unanswered-thread trends (minimum-data gated) |
| `/mission` | moderator | implemented | Run a bounded read-only mission (community analysis, web research, or draft preparation) with live progress |
| `/missions` | moderator | implemented | List recent missions in this server |
| `/mission-view` | moderator | implemented | Show one mission's steps, trace and evidence references |
| `/mission-cancel` | moderator | implemented | Request cooperative cancellation of a planned/running mission |
| `/mission-control` | moderator | implemented | Mission status, provider availability and recent activity |
| `/agent` | moderator | implemented | Plan and run a bounded dependency-aware set of read-only skills across the eight specialist roles, with live step progress |
| `/agent-runs` | moderator | implemented | List recent host-agent runs in this server |
| `/agent-view` | moderator | implemented | Show one run's steps, dependencies, outputs, evidence and errors |
| `/agent-cancel` | moderator | implemented | Cancel a planned/running host-agent run |
| `/agent-control` | moderator | implemented | Agent counts, roles, provider availability and recent runs |
| `/radar` | moderator | implemented | Evidence-backed recurring questions/errors, open threads and knowledge gaps |
| `/research` | moderator | implemented | Search the configured web provider and cite its sources (off unless configured) |
| `/research-draft` | moderator | implemented | Turn a completed research report into a Content Studio draft (never publishes) |
| `/evaluate` | configured devs | implemented | Run the offline contract, safety, isolation and regression suite |
| `/evaluation-latest` | configured devs | implemented | Show the last persisted offline evaluation report |
| `/pulse` | moderator | implemented | Evidence-backed observations + explicitly estimated recommendations (minimum-data gated) |
| `/pulse-draft` | moderator | implemented | Create a Content Studio draft from one current Pulse recommendation; never submits or publishes |
| `/knowledge-candidates` | moderator | implemented | List unapproved knowledge candidates and stale-source flags |
| `/knowledge-candidate-propose` | moderator | implemented | Accept a candidate from a stored summary (stored unapproved) |
| `/knowledge-candidate-approve` | moderator | implemented | Approve one candidate through the existing solution/KB lifecycle |
| `/knowledge-candidate-reject` | moderator | implemented | Reject one candidate; it stays stored for audit and is not searchable |
| `/knowledge-purge` | moderator | implemented | Permanently delete one approved knowledge document by `doc N` id (guild-scoped, audited, FTS-safe) |
| `/debug` | configured devs | implemented | Explain an error or stack trace (never executes anything) |
| `/forget` | moderator | implemented | Delete a thread's stored content and records (audit/workflow history retained) |
| `/conversation-status` | everyone | implemented | Show counts/timestamps of your own stored chat memory (never message text) |
| `/clear-conversation` | everyone | implemented | Delete your own stored @mention chat history in a channel (defaults to the current one) |
| `/content-create` | moderator | implemented | Generate a bounded draft for an explicit target channel; no posting |
| `/content-list` | moderator | implemented | Approval inbox filtered by status |
| `/content-view` | moderator | implemented | Exact draft text, target, version, findings and approval state |
| `/content-edit` | moderator | implemented | Replace text/target with a new immutable version (old approval cancelled) |
| `/content-submit` | moderator | implemented | Validate the exact version/target and request another moderator's approval |
| `/content-approve` | moderator | implemented | Approve the exact pending version/target (its author cannot self-approve) |
| `/content-reject` | moderator | implemented | Reject a pending request with an optional note |
| `/content-cancel` | moderator | implemented | Cancel a pending/approved request |
| `/content-publish` | moderator | implemented | Publish an approved draft immediately (disabled by default) |
| `/content-recover` | moderator | implemented | Reconcile an ambiguous send only if the nonce-proven message exists |
| `/calendar` | moderator | implemented | Show the informational content calendar and any linked scheduled job |
| `/calendar-add` | moderator | implemented | Add one planned calendar entry (schedules nothing) |
| `/calendar-cancel` | moderator | implemented | Cancel one planned calendar entry and its linked scheduled job |
| `/calendar-schedule` | moderator | implemented | Link a calendar entry to a durable read-only analysis or approval-gated draft job |
| `/schedule-agent` | moderator | implemented | Schedule a read-only host-agent run for a future time |
| `/schedule-digest` | moderator | implemented | Schedule a read-only community digest run for a future time |
| `/schedule-draft` | moderator | implemented | Schedule creation of an approval-gated Content Studio draft |
| `/scheduled-jobs` | moderator | implemented | List durable scheduled jobs for this server |
| `/schedule-cancel` | moderator | implemented | Cancel a pending scheduled job |
| `/moderation-policy` | moderator | implemented | View/configure per-guild moderation thresholds and auto-delete |
| `/moderation-status` | moderator | implemented | Show moderation policy, incident counts and recent cases |
| `/moderation-review` | moderator | implemented | Confirm, dismiss or override one incident with a note |
| `/event-plan` | moderator | implemented | Create a deterministic event plan template (creates nothing in Discord) |
| `/event-plans` | moderator | implemented | List event plans |
| `/event-plan-view` | moderator | implemented | View one event plan |
| `/event-plan-archive` | moderator | implemented | Archive one draft event plan |
| `/reply-draft` | moderator | implemented | Draft (never send) a contextual reply from a pasted excerpt |
| `/reply-drafts` | moderator | implemented | List open reply drafts |
| `/reply-view` | moderator | implemented | View one reply draft |
| `/reply-dismiss` | moderator | implemented | Dismiss one reply draft |

Content Studio publishing is **not live-verified** and is disabled unless
`CONTENT_PUBLISHING_ENABLED=true`. Drafts are generated locally, reviewed by a human
other than the author of that version, and sent only through the stored-approval path.
The durable scheduler can prepare a draft or run read-only analysis, but it has no publish
job kind and never bypasses approval.

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
| [AUDIT.md](AUDIT.md) | Audit contents, privacy rules, and retention policy |
| [CONFIGURATION.md](CONFIGURATION.md) | Every environment variable |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Dev workflow, tests, lint |
| [TESTING.md](TESTING.md) | Test strategy + how to run |
| [DEMO_GUIDE.md](DEMO_GUIDE.md) | College presentation runbook |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Fresh-machine / second-laptop guide + troubleshooting |
| [CHANGELOG.md](CHANGELOG.md) | What changed, per phase |
| [E2E_TEST_RUNBOOK.md](E2E_TEST_RUNBOOK.md) | Consolidated live E2E plan and deferred checks — **live E2E not yet run** |

## Privacy in one paragraph

Summaries are ephemeral (only the requester sees the reply) when Discord allows it. But an
ephemeral reply does **not** make the source messages private from the bot: it reads the
thread, may store a bounded excerpt locally (SQLite) for features you asked for, and keeps
it only within the configured retention window. Moderators can delete a thread's data with
`/forget`, or one approved knowledge document with `/knowledge-purge` (shown as `doc N` in
`/kb-search`). @mention chat turns are stored the same way — scoped strictly per user and
channel, bounded by age and row caps, and deletable by you with `/clear-conversation`.
Approved knowledge survives automatic thread expiry; audit rows are append-only and retained
as decision/deletion evidence ([AUDIT.md](AUDIT.md)). Missions, research reports, calendar
entries, event plans and reply drafts are stored guild-scoped; missions are read-only, and
reply drafts are never sent automatically. Web research is off by default; when enabled, the
search query (not community message content) is sent to the configured provider, and only
provider-returned URLs are cited. When multiple guilds are allowlisted,
knowledge-base search/grounding, related items, `/forget`, `/knowledge-purge`, insights,
missions, research, radar and planning data are isolated per guild. Details and limits:
[SECURITY.md](SECURITY.md).
=======
# ai-copilot-assistant
An AI-Powered Community Copilot
>>>>>>> 648756824e91f4875ff1711ee83048d7f69f4b26
