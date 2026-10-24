# Community Copilot — Consolidated Live E2E Runbook

> ## STATUS: **NOT RUN / BLOCKED**
> As of **2026-10-24**, the consolidated live end-to-end test has **not been executed** because
> this phase is offline-only and no live Discord/provider action was authorized.
> This document is a plan for a future, explicitly authorized session. No bot login, slash-command
> sync, real Discord command run, real Brave/Tavily call, or production database migration has been
> performed for the program phases below. Automated offline coverage is recorded in
> [TESTING.md](TESTING.md) and [PLAN.md](PLAN.md); it is **not** a substitute for this runbook.
> The offline suite at the time of writing: **753 passed, 2 skipped, 1 warning, exit 0**
> (2026-10-24; JUnit `tests=755`, `failures=0`, `errors=0`; skips are the opt-in live-Ollama
> tests; the warning is the third-party `audioop` deprecation).
>
> Coverage labels used below:
> - **TESTED (offline)** — covered by automated offline tests; still requires live confirmation here.
> - **MANUAL ONLY** — no automated coverage; the live result is the only evidence.
> - **SKIPPED (by default)** — deliberately not part of this run unless explicitly authorized.
> - **UNVERIFIED** — not tested offline or live; do not claim it works.
> - **BLOCKED** — cannot be run without a separate authorization, token, test guild, provider
>   credential, or live Discord action. A blocked item is not a pass or a failure.
>
> Per-phase truth remains in [PLAN.md](PLAN.md). Do not mark a runbook step as passed by reading
> this file — record the actual result and evidence in §14.

### Coverage snapshot

| Feature | Offline coverage | Live run |
|---|---|---|
| Core summary/knowledge/insights | TESTED (offline) | UNVERIFIED |
| Missions | TESTED (offline) | UNVERIFIED |
| Web research | TESTED (offline, unavailable/disabled paths) | SKIPPED (by default) |
| Radar / evaluation | TESTED (offline) | UNVERIFIED |
| Planning / reply drafts | TESTED (offline) | UNVERIFIED |
| Held-reply approvals (`REPLY_APPROVAL_ENABLED`) | TESTED (offline) | SKIPPED (by default; approval sends) |
| Content Studio + publishing safety | TESTED (offline) | UNVERIFIED (publishing disabled) |
| Agent runs | TESTED (offline) | UNVERIFIED |
| AI moderation | TESTED (offline) | MANUAL ONLY (mention-only; auto-delete off by default) |
| Attachment ingestion + vision | TESTED (offline, magic bytes / octet-stream) | UNVERIFIED (model compatibility) |
| Scheduler | TESTED (offline) | SKIPPED (by default) |

## 1. Purpose and scope

Verify, against a **disposable Discord test server** and a **scratch database**, that:

1. The bot starts with only non-privileged intents, registers the expected slash commands, and
   refuses non-allowlisted guilds and non-moderators.
2. Missions run fixed read-only plans with live progress, evidence references, cancellation,
   and honest failure/recovery behavior.
3. Web research is off/unavailable by default, and when configured returns real provider
   citations with no fabricated URLs.
4. Problem Radar reports evidence and uncertainty honestly, including the insufficient-data state.
5. Offline evaluation runs without touching Discord/Ollama/providers and persists a truthful report.
6. Calendar entries, event plans and reply drafts create nothing public; a planned entry can be
   linked to a durable **read-only/draft-only** scheduled job only via explicit
   `/calendar-schedule` (Phase I, out of scope by default).
7. Existing approval/Content Studio behavior still works with **publishing disabled**, and
   multi-guild isolation holds for all new data.
8. Logs and audit rows contain no secrets, tokens, API keys, or research-query text in audit metadata.
9. Agent runs, AI moderation review, attachment ingestion and vision handling behave as documented
   (Phases F–H).

Out of scope for this run unless a separate explicit authorization is recorded first:

- enabling `CONTENT_PUBLISHING_ENABLED`, posting to production channels, real-user data, and
  model-quality evaluation;
- **all scheduler execution** (`/schedule-agent`, `/schedule-digest`, `/schedule-draft`,
  `/calendar-schedule`, `/scheduled-jobs`, `/schedule-cancel`) — Phase I is a checklist only and
  must not be marked tested by running this document;
- Sending a held mention reply with `/reply-approve` while `REPLY_APPROVAL_ENABLED=true`. That
  command posts the exact stored text to the stored channel, so it is a public send and needs a
  separate explicit approval. Keep `REPLY_APPROVAL_ENABLED=false` for the standard run.
- AI moderation auto-deletion unless a separate explicit approval is recorded. The bot has no
  mute, kick or ban path. Auto-delete is disabled per guild by default, requires a configured
  policy above its severity/confidence thresholds, the bot's Manage Messages permission, and the
  per-author daily cap. Even when enabled, the report describes the policy rather than claiming
  every incident is deleted.

## 2. Safety rules (read before starting)

- Use a **disposable test guild** and scratch channels. Do not run this in a real community.
- Use a **scratch database**, never the production one:
  `DATA_DIR=data/e2e-<date>`, `DB_FILENAME=copilot-e2e.db`. Do not copy `data/copilot.db`,
  WAL/SHM files, or backups into the run.
- Keep `CONTENT_PUBLISHING_ENABLED=false` for the entire run unless a separate, explicit
  publish-only approval is obtained. Keep `REPLY_APPROVAL_ENABLED=false` in the standard run:
  approving a held mention reply posts it publicly. Without a separate send approval this runbook
  never posts a public message.
- Keep `WEB_RESEARCH_ENABLED=false` until §6, and restore it to `false` afterward.
- Set `SCHEDULER_ENABLED=false` for the standard run. The setting defaults to `true`; leave all
  `/schedule-*` and `/calendar-schedule` commands untouched — they are Phase I (skipped by
  default) and require a separate explicit authorization.
- Never paste the Discord token or provider keys into Discord, logs, screenshots, or this file.
- Only `/forget` and `/knowledge-purge` rows created by this run may be deleted.
- Stop and roll back (see §13) on any unexpected public message, cross-guild data visibility,
  secret in logs, or destructive behavior.

## 3. Preconditions

| Item | Requirement |
|---|---|
| Bot | Test application token in `.env` (operator-entered; never shared); invited with `bot` + `applications.commands`, permissions 84992 |
| Guild | One disposable guild for the main run; a **second** disposable guild if multi-guild isolation is tested |
| Roles | Account A: moderator; Account B: second moderator (approval tests); Account C: regular member; Account D: configured dev user for `/evaluate` |
| Ollama | `ollama serve` running; `gemma4:e2b` present; `/status` reports model OK |
| Data | At least `INSIGHTS_MIN_THREADS` (default 5) summarized threads in the test guild before Radar/Pulse steps |
| Research (optional) | A **test** Brave or Tavily key, if §6 is in scope; otherwise research stays disabled |
| Files | This runbook, [CONFIGURATION.md](CONFIGURATION.md), [DEPLOYMENT.md](DEPLOYMENT.md) |

### Practical manual sequence (start here)

Use this as the run order; the detailed expectations are in the phase sections below.

1. Create a disposable guild/channels and a scratch `DATA_DIR`; do **not** point at production data.
2. Set the scratch env: guild allowlist, `TEST_GUILD_ID`, dev users, `CONTENT_PUBLISHING_ENABLED=false`,
   `REPLY_APPROVAL_ENABLED=false`, `WEB_RESEARCH_ENABLED=false`, `SCHEDULER_ENABLED=false`.
3. Run `scripts/check_prereqs.py` and record the output.
4. Start the bot and record `/status`, the loaded cogs, migration version **v10**, and the command inventory.
5. Run Phase A authorization checks: non-moderator refusal, non-allowlisted guild refusal.
6. Seed at least `INSIGHTS_MIN_THREADS` summarized threads in the test guild.
7. Run Phase B missions, including one cancellation and the disabled-research failure path.
8. Run Phase D radar/evaluation/planning (research stays disabled unless separately approved).
9. Run Phase E Content Studio approval regression; confirm `/content-publish` refuses and posts nothing.
10. Run Phase F agent runs and one restart-recovery check.
11. Run Phase G AI moderation with `delete_enabled:false`; do not grant Manage Messages for the standard run.
12. Run Phase H attachments, including the same PNG as `upload.bin`/`application/octet-stream`.
13. Skip Phase C research and Phase I scheduler unless separate authorizations are recorded.
14. Fill in §14 with the actual result for every phase; do not infer a pass from offline tests.


## 4. Phase A — startup, permissions, command surface

1. Set the scratch `DATA_DIR`/`DB_FILENAME`, `GUILD_ALLOWLIST` (test guild), `TEST_GUILD_ID`,
   and the dev-user list. Leave publishing and research disabled.
2. Run `scripts/check_prereqs.py`; record its output (redact paths if needed).
3. Start the bot. Confirm in logs: no privileged intents, DB migration reaches **v10** on the
   scratch file, cogs load, commands sync without errors.
4. Confirm the complete registered command inventory (registration only; registration is **not**
   a test pass):

   | Group | Commands |
   |---|---|
   | Core | `/status`, `/summarize`, `/cancel`, `/debug`, `/forget` |
   | Knowledge | `/kb-search`, `/resolve`, `/approve-solution`, `/reject-solution`, `/correct-status`, `/knowledge-candidates`, `/knowledge-candidate-propose`, `/knowledge-candidate-approve`, `/knowledge-candidate-reject`, `/knowledge-purge` |
   | Insights | `/insights`, `/pulse`, `/pulse-draft` |
   | Missions | `/mission`, `/missions`, `/mission-view`, `/mission-cancel`, `/mission-control` |
   | Research / radar | `/research`, `/research-draft`, `/radar` |
   | Evaluation | `/evaluate`, `/evaluation-latest` |
   | Planning | `/calendar`, `/calendar-add`, `/calendar-cancel`, `/event-plan`, `/event-plans`, `/event-plan-view`, `/event-plan-archive`, `/reply-draft`, `/reply-drafts`, `/reply-view`, `/reply-dismiss` |
   | Reply approvals | `/reply-approvals`, `/reply-approve`, `/reply-reject` |
   | Agents | `/agent`, `/agent-runs`, `/agent-view`, `/agent-cancel`, `/agent-control` |
   | Moderation | `/moderation-policy`, `/moderation-status`, `/moderation-review` |
   | Content | `/content-create`, `/content-list`, `/content-view`, `/content-edit`, `/content-submit`, `/content-approve`, `/content-reject`, `/content-cancel`, `/content-publish`, `/content-recover`, `/content-feedback`, `/voice-show`, `/voice-set` |
   | Conversation | `/conversation-status`, `/clear-conversation` |
   | Scheduler (out of scope, Phase I) | `/schedule-agent`, `/schedule-digest`, `/schedule-draft`, `/calendar-schedule`, `/scheduled-jobs`, `/schedule-cancel` |

5. **Expected:** `/evaluate` and `/evaluation-latest` are dev-only; mission/research/radar/
   planning/Content Studio/agent/moderation commands are moderator-only.
6. Account C tries `/mission`, `/radar`, `/research`, `/calendar`, `/reply-draft`, `/agent`,
   `/moderation-status`: **expected** a moderator-permission refusal and no data change.
7. If a second non-allowlisted guild is available, run `/status` there: **expected** refusal.

## 5. Phase B — missions

Use the seeded test guild.

1. As moderator: `/mission mode=Community analysis goal=What are the most common unresolved problems this month?`
   - **Expected:** immediate deferred reply, live progress embed, steps run in fixed order
     (pulse → insights → KB search), terminal status `completed` or an honest safe failure;
     each step shows evidence references where available.
2. `/missions` then `/mission-view mission_id=<id>`:
   - **Expected:** the mission appears guild-scoped; the view shows the trace id, step statuses,
     references and no other guild's rows.
3. `/mission-control`:
   - **Expected:** provider availability, recent activity, truthful counts; research shows
     unavailable while disabled.
4. Cancellation: start another `/mission`, then quickly `/mission-cancel mission_id=<id>`.
   - **Expected:** cancellation is requested cooperatively; terminal status `cancelled` (or
     `completed` if it finished first — record which); no mission remains `running`.
5. Failure path: `/mission mode=Web research goal=...` while `WEB_RESEARCH_ENABLED=false`.
   - **Expected:** the research step fails/returns unavailable honestly; the mission does not
     fabricate sources; progress count is honest (e.g. `0/2`, not `2/2`).
6. Restart recovery: start a mission whose steps take long enough, restart the bot mid-run.
   - **Expected:** after restart, the mission is terminal `failed`/`interrupted`; unfinished steps
     are `cancelled`; it never stays `running`. Re-check `/mission-view`.
7. Long goal (> `MISSION_MAX_GOAL_CHARS`, default 500) is rejected before storing.

## 6. Phase C — web research (optional, explicit approval)

Skip this phase entirely if no test provider key is available; record it as **skipped**.

1. Keep research disabled: `/research query=Redis connection pool exhaustion`.
   - **Expected:** an `unavailable` result with **no citations and no invented sources**.
   - `/research-draft report_id=<that report>` must refuse (not completed).
2. Only with an explicitly approved test key: set `WEB_RESEARCH_ENABLED=true`,
   `WEB_SEARCH_PROVIDER=brave|tavily`, and the test key outside Discord. Restart if required.
3. `/research query=<bounded technical query>`.
   - **Expected:** a completed report with citations whose URLs match provider results; a
     synthesis label (`model` or `extractive`); no page contents fetched.
   - Click/inspect one citation: it must be the provider-returned URL, never model-invented.
4. `/research-draft report_id=<id> channel=#test-channel`:
   - **Expected:** a Content Studio draft is created with research provenance; it is **not**
     submitted, approved or published. Confirm no message appeared in the target channel.
5. Try `/research-draft` with a report id from another guild: **expected** not found.
6. Restore `WEB_RESEARCH_ENABLED=false` and remove the test key from the scratch environment
   after the phase. Record whether a real provider call was made.

## 7. Phase D — Radar, evaluation and planning

### Radar

1. In a guild below the minimum-data threshold: `/radar`.
   - **Expected:** explicit insufficient-data state; no invented trends or counts.
2. After seeding ≥ `INSIGHTS_MIN_THREADS` summaries: `/radar`.
   - **Expected:** recurring questions/errors, open threads and approved-knowledge gaps with
     evidence counts, low/medium/high confidence, uncertainty labels and a recommended action;
     no member names or profiles.
3. Cross-guild: seed a different guild and verify its items never appear here.

### Evaluation

1. As the configured dev: `/evaluate`.
   - **Expected:** an offline report with passing checks, `offline_only=True` and
     `model_quality_evaluated=False`; no Discord/Ollama/provider call beyond normal bot health.
2. `/evaluation-latest`: **expected** the same persisted report for this guild.
3. As a moderator who is not a dev: `/evaluate` → **expected** dev-permission refusal.
4. Confirm the scratch database's user data was not modified by evaluation (evaluation uses
   temporary databases by design).

### Planning

1. `/calendar-add title=Release note kind=announcement starts_at=2026-11-05T18:00:00+00:00`
   → `/calendar` shows the entry; **expected** nothing was posted and no scheduled job exists yet.
   A calendar entry becomes a scheduled job **only** through the explicit `/calendar-schedule`
   command (Phase I, out of scope by default).
2. `/calendar-cancel entry_id=<id>` → state `cancelled`; repeating the same cancel must fail
   safely (conditional transition), not silently "succeed". This also cancels a linked pending
   scheduled job if one exists; do not test that link in this phase.
3. `/event-plan title=Office hours brief=Monthly community Q and A` → `/event-plans`,
   `/event-plan-view`, `/event-plan-archive`; repeat archive must fail safely. **Expected:**
   nothing was created in Discord.
4. `/reply-draft source=@everyone can someone help with this? purpose=Offer troubleshooting steps`:
   - **Expected:** one bounded draft; mentions neutralized (no pinging `@everyone`); stored as
     `draft`; **never sent**. `/reply-view`, `/reply-drafts`, `/reply-dismiss` work; a second
     dismiss fails safely. Reply drafts are not approval records: `/reply-approve` does not apply
     to them.
5. Held-reply approvals are a **separate, opt-in** feature and are **SKIPPED** by default. With
   `REPLY_APPROVAL_ENABLED=false`, `/reply-approvals`, `/reply-approve` and `/reply-reject` must
   reply that approval is disabled. Only if a separate send approval is recorded: set
   `REPLY_APPROVAL_ENABLED=true` in the scratch environment, @mention the bot to generate a held
   reply, verify it is **not** posted and does **not** appear in `/conversation-status`, then run
   `/reply-approvals`. `/reply-reject` discards it; `/reply-approve` **sends the exact stored text
   to the stored channel** and reports how many chunks were delivered. At send time the TTL and
   the channel allowlist are rechecked; an expired or now-disallowed reply is marked `failed` and
   not sent. The decision and its audit row are committed atomically. If the process stops after
   that commit but before the send, the row can remain `approved` and is **not** resent
   automatically (at-most-once). Never run this subtest while posts are unauthorized.
6. Cross-guild: attempt `/reply-view`/`/event-plan-view`/`/calendar-cancel` with another guild's
   ids → **expected** not found.

## 8. Phase E — approval regression and multi-guild isolation

1. Keep publishing disabled. `/content-create` → `/content-submit`:
   - **Expected:** submission succeeds as a pending approval; the version author cannot approve it.
   - Manual content is trimmed at creation/edit, so leading/trailing whitespace cannot make the
     approval hash mismatch later. Confirm the stored version and approval hash still agree.
2. Account B (different moderator) `/content-approve`: **expected** approval succeeds.
3. `/content-publish` with `CONTENT_PUBLISHING_ENABLED=false`: **expected** a clear refusal
   **before** any claim/send; **no Discord message** appears.
4. `/content-reject` or `/content-cancel` per status; verify buttons/state behave consistently.
5. Multi-guild checks: allowlist both test guilds, then verify `/kb-search`, `/pulse`, `/radar`,
   `/missions`, `/research-draft`, `/calendar`, `/event-plans`, `/reply-drafts`,
   `/evaluation-latest`, `/agent-runs`, `/moderation-status` never show the other guild's rows.
   Cross-guild thread targets are refused. If the opt-in held-reply subtest was approved, also
   check `/reply-approvals`; otherwise record it **SKIPPED (disabled)**.
6. Conversation memory view: @mention the bot in a test channel, then run
   `/conversation-status`. **Expected:** your own scope only, turn count and timestamp bounds,
   SQLite as the source, no message text. `/clear-conversation`, then check the status again:
   **expected** 0 turns. A channel from the other guild is refused.
7. `/forget` on a thread that has stored content: **expected** the reply is deferred, the content is
   deleted, and exactly one `thread.forgotten` audit row is written in the same transaction. On a
   thread with nothing stored, **expected** the honest "Nothing was stored" reply (the audit row is
   still written). `/clear-conversation` removes conversation turns and the caller's attachment
   rows in that scope, but deliberately does **not** delete approval or audit records.
8. Restart the bot and confirm new rows persist, and an interrupted mission is recovered as
   `failed`/`interrupted` (repeat §5.6 if not already done).

## 9. Phase F — agent runs

Coverage: **TESTED (offline)** for all six modes, contracts/isolation/recovery; live behavior
**UNVERIFIED**.

1. As moderator: `/agent mode=Community analysis goal=Summarize this month's unresolved issues`.
   - **Expected:** a bounded, fixed read-only plan runs; terminal `completed` or an honest safe
     failure; no message is posted, no moderation action, and no draft is published.
2. `/agent-runs` then `/agent-view run_id=<id>`:
   - **Expected:** guild-scoped run with steps, dependencies and evidence references; no other
     guild's rows.
3. `/agent-control`: **expected** truthful role/provider availability and recent run counts.
4. Cancellation: start a run, then `/agent-cancel run_id=<id>`.
   - **Expected:** cooperative cancellation; terminal `cancelled`, or `completed` if it finished
     first (record which); it never stays `running`.
5. Restart recovery: interrupt a run by restarting the bot.
   - **Expected:** startup recovery marks planned/running runs `failed`/`interrupted`; no run is
     left `running`.
6. Account C (non-moderator) runs `/agent`: **expected** refusal and no run created.
7. `mode=Moderation review` with a harmless excerpt: **expected** a read-only classification,
   no incident row, no deletion/warning and no `moderation.*` audit. The dedicated mention-only
   moderation workflow remains the only path that can act.
8. `mode=Troubleshooting triage` with a pasted traceback: **expected** lexical error evidence and
   safe next steps, not a verified root cause. A screenshot through @mention chat is the vision
   path; the host does not claim to have read an image.
9. `mode=Draft preparation`: **expected** a grounded draft suggestion, **no** Content Studio
   draft row, no approval and no publication. Confirm `/content-drafts` did not gain a row.
10. `mode=Quality evaluation`: **expected** offline checks pass/fail honestly, with **no**
    persisted evaluation report (unlike `/evaluate`).
11. Cross-guild: `/agent-view` with another guild's run id → **expected** not found.

## 10. Phase G — AI moderation

Coverage: **TESTED (offline)** for policy/incident/review contracts; live Discord behavior
**MANUAL ONLY** (requires the bot to receive a mention).

1. With `MODERATION_ENABLED=true` in the scratch environment (it is `false` by default), as
   moderator run `/moderation-policy enabled:true report_channel:#mod-log delete_enabled:false`.
   Do **not** grant Manage Messages for the standard run. `/moderation-status` must show
   `Delete automatically: no` and the configured thresholds. Re-running
   `/moderation-policy enabled:true` with the other options omitted must **preserve** the report
   channel, thresholds, category list, delete flag, daily cap and notify flag.
2. In the test channel, @mention the bot with text that trips the configured policy.
   - **Expected with `delete_enabled:false`:** an incident is recorded and reported to the
     private channel; the message is **not** deleted. The bot has no mute, kick or ban path.
3. `/moderation-review incident_id=<id> decision=confirm|dismiss|override`:
   - **Expected:** the review is recorded with the reviewer; repeating a decided review fails
     safely.
4. Ordinary (non-mention) channel messages are out of scope because the Message Content intent
   is disabled; do not interpret their absence from moderation as a failure.
5. Cross-guild: `/moderation-review` with another guild's incident id → **expected** not found.
6. **Auto-delete subtest — only with explicit approval.** Re-run `/moderation-policy` with
   `delete_enabled:true` and this time grant Manage Messages to the bot in the test guild. Post
   another tripping mention. **Expected:** the message is deleted only when its category is
   configured, the assessment clears both thresholds, the daily cap is not hit, and the bot can
   delete; the incident records `action=deleted`. A lower-severity, low-confidence, uncategorized
   or over-cap mention stays flagged and is only reported. When the daily cap is reached,
   automatic user warnings/DMs are also paused, not just deletion. Set `delete_enabled:false`
   immediately afterward and remove Manage Messages if it was not already granted.

## 11. Phase H — attachments and vision

Coverage: **TESTED (offline)** for magic-byte classification and the octet-stream PNG path; live
model compatibility **UNVERIFIED** — record the configured model and whether it reports vision.

1. @mention the bot with a small PNG image:
   - **Expected:** if the configured model reports vision support, the image reaches the model and
     the reply reflects it. If it does not, the bot returns a clear "vision unsupported" note and
     never claims the image was analysed. Re-encoded images are orientation-corrected and carry no
     EXIF metadata.
2. Repeat with the **same PNG bytes** uploaded as `upload.bin` / `application/octet-stream`.
   - **Expected:** the file is still classified as an image by magic bytes (not by filename or
     MIME type) and follows the same path as step 1. The same generic-MIME candidate path applies
     to `/summarize` image attachments; a known document/text suffix is not treated as an image.
3. @mention the bot with a text or PDF attachment:
   - **Expected:** bounded extracted text is used; raw binary is never echoed; size/page caps are
     enforced and a skipped file is reported, not silently dropped. Text containing
     `</attachments>` or `<<<`/`>>>` is escaped and cannot close or reopen the untrusted fence.
4. `/conversation-status`, then `/clear-conversation`:
   - **Expected:** status shows only turn counts and timestamp bounds (it does not claim to count
     attachments). Clearing removes the caller's turns **and** that caller's attachment
     metadata/extracted text in the channel; when attachment rows were removed the confirmation
     says how many. No other guild, channel or user scope is affected. Check status again: 0
     turns.
5. Attachment age expiry (daily `ATTACHMENT_RETENTION_DAYS` sweep) cannot be observed within a
   short live run; record it as **UNVERIFIED live** (covered offline).

## 12. Phase I — scheduler (out of scope by default)

**Status: SKIPPED (by default) / UNVERIFIED live.** Do **not** run `/calendar-schedule` or any
`/schedule-*` command in the standard run, and do not record them as tested. They are listed here
so a separately authorized session has a checklist.

If (and only if) scheduling is explicitly authorized for the test guild, set
`SCHEDULER_ENABLED=true` in the scratch environment and restart:

1. `/schedule-agent goal=... when=<a few minutes ahead> timezone=UTC`: `/scheduled-jobs` shows a
   pending job; after the due time it reaches a terminal state. No message is posted. A job that
   is already due at startup runs only after the gateway cache is ready; it must not fail with a
   permanent authorization error just because the bot started.
2. The slash command uses its interaction id as the idempotency key. A retried delivery of the
   same interaction must return the same job id; two separate `/schedule-agent` invocations are
   allowed to create two jobs. Record which behavior was observed.
3. `/schedule-cancel job_id=<id>` cancels a pending job, and cancels an in-flight job without a
   later success/failure overwriting the cancelled row.
4. Revoke the requester's moderator role after scheduling: at execution the job fails permanently
   with `authorization_revoked`; no agent/draft work runs; audit contains
   `scheduler.job_authorization_revoked` with the guild id. A transient Discord/cache lookup
   failure is different: the job stays pending with
   `authorization_temporarily_unavailable` and retries; it is not recorded as a revocation.
5. With a non-empty `CHANNEL_ALLOWLIST` that initially includes the target, remove the target
   while leaving at least one other channel listed: the job fails permanently with
   `channel_not_allowed` and performs no work. (An empty allowlist means "all channels", not
   "none".)
6. Restart with a job claimed/running: recovery returns it to pending without granting an extra
   attempt, clears stale `started_at`/`finished_at`, and rows at the attempt cap become `failed`.
7. `/schedule-draft`: exactly one draft exists per job (`source_kind=scheduled`,
   `source_ref=job:<id>`), even after a retry/recovery; the draft still requires human approval.
   Draft preparation uses the target snapshot stored when the job was created; approval and
   publication revalidate the live target, so a deleted channel cannot be published to.
8. `/calendar-schedule entry_id=<planned id>`: links the planned entry to a read-only/draft-only
   job; it does not publish, create a Discord event, or send a message. If the entry's target
   channel is deleted or is not a text channel, the command fails with a clear error instead of
   silently retargeting the current channel.
9. No scheduled job kind can publish content or perform a moderation action.

## 13. Abort and rollback

Stop the run and report immediately if you observe any of:

- a public message posted by a planning/mission/research/agent/scheduler command;
- another guild's data visible;
- a token/key/secret in logs or Discord;
- a mission or agent run stuck `running` after restart;
- an approval executed while publishing is disabled;
- a message deleted while `delete_enabled:false`, or any mute/kick/ban taken by the bot;
- a scheduled job executing after its requester or channel authorization was revoked.

Rollback for the test only: stop the bot, then remove the **scratch** `DATA_DIR` directory and its
logs/log files created by this run. Do **not** delete `data/copilot.db`, WAL/SHM files, backups, or
any production data. Revoke the test provider key if one was created.

## 14. Result record

Fill in on the authorized run. Do not pre-fill "passed". Use the status labels from the header:
`TESTED (offline)`, `MANUAL ONLY`, `SKIPPED (by default)`, `UNVERIFIED`, `BLOCKED`, `PASS`,
`FAIL`, `NOT RUN`.

| Date | Phase | Executed by | Result (actual) | Evidence (log excerpt/screenshot path, redacted) |
|---|---|---|---|---|
| | A startup/permissions | | **NOT RUN** | |
| | B missions | | **NOT RUN** | |
| | C research | | **SKIPPED (by default)** unless a test provider key is approved | |
| | D radar/evaluation/planning | | **NOT RUN** | |
| | E approval/isolation/restart | | **NOT RUN** | |
| | F agent runs | | **NOT RUN** | |
| | G AI moderation | | **NOT RUN** / **BLOCKED** (live Discord) | |
| | H attachments/vision | | **NOT RUN**; vision compatibility **UNVERIFIED** until tested | |
| | I scheduler | | **SKIPPED (by default)**; commands **UNVERIFIED** live | |

**Overall consolidated live E2E status: NOT RUN / BLOCKED (2026-10-24).** The offline suite is
recorded in [TESTING.md](TESTING.md); live results require a separately authorized run.

## 15. Known expected limitations during the run

- Missions are fixed, read-only plans; there is no free-form autonomous hypothesis loop.
- `/research` requires a configured provider and key; without them `unavailable` is the correct
  outcome, not a bug.
- Provider result quality and rate limits are unmeasured; citations are only as good as the
  provider's results, and synthesis can fall back to extractive mode.
- Radar is lexical/count-based; paraphrases can be missed and confidence reflects sample size.
- Evaluation validates contracts/safety/isolation, **not** model answer quality.
- Calendar entries, event plans and reply drafts create nothing public. A planned calendar entry
  becomes a durable **read-only/draft-only** job only via explicit `/calendar-schedule`; that
  command is out of scope here and no job kind can publish.
- Content publishing remains at-most-once per claim and requires human approval; it is out of
  scope for this run while disabled.
- Vision support depends on the configured Ollama model. Offline tests prove magic-byte
  classification and the `application/octet-stream` path, not that a specific model understands
  images; do not claim vision compatibility without Phase H.
- AI moderation is mention-only because the Message Content intent stays off. Auto-delete is a
  per-guild opt-in; it requires Manage Messages, clears both thresholds and respects the daily
  cap. Below-threshold, ambiguous or over-cap assessments are reported, not deleted. There is no
  mute, kick or ban path. A model-returned category that is not in the policy is treated as
  `other`; `other` must be in the configured list for an automatic action. Omitting
  `/moderation-policy` options preserves the existing values.
- `/clear-conversation` deletes conversation turns and the caller's attachment rows in that
  channel/user scope. Approval and audit rows intentionally survive; pending held replies are not
  a conversation-history entry and are managed with `/reply-reject` (or expire on their TTL).
- Held-reply delivery is at-most-once. The approval decision and audit are atomic, but if the
  process stops between that commit and the send, the row can remain `approved` without being sent;
  it is not automatically resent. Record it as **UNVERIFIED/BLOCKED** if that window is hit.
- Scheduled draft preparation uses the target snapshot captured at scheduling; only the final
  approval/publish path rechecks the live channel. A deleted target can still produce an
  unpublished draft, but cannot be published.
- Agent, moderation and scheduler live behavior is **UNVERIFIED** until the corresponding phase
  is actually run and recorded; command registration is not a test pass.
