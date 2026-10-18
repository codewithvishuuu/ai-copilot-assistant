# Community Copilot — Product Requirements (PRD)

Scope: a **Discord-only** local-first assistant. Acceptance criteria are phase-linked to
[PLAN.md](PLAN.md). "MVP" = Phases 0–3 (+4 when hardware allows).

## Feature requirements & acceptance criteria

| # | Feature | Phase | Acceptance criteria (how we know it works) |
|---|---|---|---|
| 1 | `/summarize` a forum thread (OP + configurable recent messages) | 2 | Works on a real thread ≤ configured caps; returns embed with overview + problem + findings + decisions + open questions + status + next step + source links; degrades with a clear message when Ollama/thread unavailable |
| 2 | Issue status detection (unresolved / in progress / waiting for user / resolved / unclear) | 3 | Status always one of the five enum values with a confidence + reason; shown as an AI suggestion, never authoritative |
| 3 | Extract unanswered questions, findings, decisions, errors, next steps | 3 | All fields present (possibly empty lists); each evidence item carries validated source-message links where available |
| 4 | Source message links for important claims | 2–3 | Every rendered link resolves to a message actually collected in this run; unknown IDs from the model are dropped before rendering |
| 5 | Screenshot/image analysis when the model supports vision | 4 | Enabled only after runtime capability check (`/api/show`); images validated (type/size/pixels), re-encoded via Pillow (EXIF stripped); skipped images are reported, never silently claimed as analyzed |
| 6 | Private (ephemeral) summaries by default | 1–2 | Replies are ephemeral when Discord allows; docs state ephemeral hides the reply, not the source data |
| 7 | Guild/channel allowlists | 1 | Commands outside allowed guilds/channels are refused with a generic message; covered by unit tests |
| 8 | Request limits, timeouts, sequential queue, cancellation, graceful errors | 2 | Queue capacity enforced; per-user cooldown + per-guild hourly cap; timeouts produce friendly messages; `/cancel` reliably cancels pending and running jobs; no traceback reaches users |
| 9 | Report skipped/unsupported messages & attachments | 2–4 | Summary embed includes an accurate "skipped" note listing counts/reasons |
| 10 | Duplicate/related issue detection over older threads | 5 | "Possibly related" list with evidence links above a threshold; never auto-merges |
| 11 | Moderator-approved solutions stored & retrievable | 5 | Proposed (`/resolve`) → not searchable as official; approved (`/approve-solution`) → searchable via `/kb-search`; rejection supported; all with actor + timestamp |
| 12 | Developer debugging helper for errors/stack traces | 6 | `/debug` explains likely causes + safe next steps; text treated as untrusted; nothing is executed or fetched; restricted to configured dev IDs |
| 13 | Community insights when sufficient data exists | 6 | Hard minimum-data threshold; below it, the command says so explicitly instead of showing noise |
| 14 | Moderator status corrections recorded | 3 | `/correct-status` stores override with actor + timestamp; summary output shows correction |
| 15 | Extensible architecture, not maximal MVP | all | Later features added as new modules against stable interfaces (documented in ARCHITECTURE.md) |

## Non-goals (explicit)

- No website, dashboard, desktop GUI, or REST API.
- No mandatory cloud AI API, cloud DB, vector DB, Docker, or background workers.
- No automated moderation actions (no closing threads, deleting messages, banning).
- No user profiling/tracking beyond what commands need.

## User stories (key ones)

1. **As a community member**, I run `/summarize` in a long help thread and get a compact, source-linked summary that tells me if the issue is still open.
2. **As a moderator**, I correct a wrong status, propose a resolution, and approve it so future members find it via `/kb-search`.
3. **As a member with an error**, I paste a stack trace into `/debug` and get likely causes + what to check next — without anyone executing it.
4. **As the maintainer**, I can run everything on a modest laptop (8 GB RAM), with no cloud dependency, and demo it on a second laptop after a checklist.

## Privacy requirements

- Only summarized threads are persisted, bounded by caps + retention window (default 90 days).
- `/forget` deletes a thread's stored data completely (messages, summaries, fingerprints; FTS rows included).
- Logs never contain message bodies at normal levels.
- See [SECURITY.md](SECURITY.md) for controls and their tests.
