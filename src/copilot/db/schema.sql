-- Community Copilot schema. Single source of truth for migrations.
-- A block starts with:  -- @migration N
-- db/database.py applies blocks with N > PRAGMA user_version, in order.

-- @migration 1
-- Core operational tables (threads summarized by the bot, moderator workflow).

CREATE TABLE IF NOT EXISTS threads (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  discord_thread_id   TEXT NOT NULL UNIQUE,
  guild_id            TEXT NOT NULL,
  parent_channel_id   TEXT,
  name                TEXT NOT NULL DEFAULT '',
  created_at          TEXT NOT NULL,
  last_summarized_at  TEXT,
  retention_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id    INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  message_id   TEXT NOT NULL,
  author_id    TEXT,
  author_name  TEXT,
  created_at   TEXT,
  content      TEXT NOT NULL DEFAULT '',
  is_opening   INTEGER NOT NULL DEFAULT 0,
  UNIQUE (thread_id, message_id)
);

CREATE TABLE IF NOT EXISTS summaries (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id         INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  created_at        TEXT NOT NULL,
  requested_by      TEXT,
  model             TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  status            TEXT NOT NULL,
  confidence        REAL,
  analysis_json     TEXT NOT NULL,
  error_signatures  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS corrections (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id   INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  status      TEXT NOT NULL,
  actor_id    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  note        TEXT
);

CREATE TABLE IF NOT EXISTS solutions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id     INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  body          TEXT NOT NULL,
  source_url    TEXT,
  state         TEXT NOT NULL DEFAULT 'proposed'
                CHECK (state IN ('proposed','approved','rejected')),
  proposed_by   TEXT NOT NULL,
  proposed_at   TEXT NOT NULL,
  decided_by    TEXT,
  decided_at    TEXT,
  note          TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at   TEXT NOT NULL,
  actor_id     TEXT NOT NULL,
  action       TEXT NOT NULL,
  target_type  TEXT,
  target_id    TEXT,
  meta_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_thread    ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_summaries_thread   ON summaries(thread_id);
CREATE INDEX IF NOT EXISTS idx_summaries_status   ON summaries(status, created_at);
CREATE INDEX IF NOT EXISTS idx_solutions_state    ON solutions(state);
CREATE INDEX IF NOT EXISTS idx_corrections_thread ON corrections(thread_id, created_at);

-- @migration 2
-- Knowledge base: searchable documents (approved solutions + thread fingerprints)
-- with an FTS5 external-content index kept in sync by triggers.

CREATE TABLE IF NOT EXISTS kb_docs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK (kind IN ('solution','thread')),
  ref_id      INTEGER,
  title       TEXT NOT NULL DEFAULT '',
  body        TEXT NOT NULL,
  url         TEXT,
  created_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
  title,
  body,
  content='kb_docs',
  content_rowid='id',
  tokenize='porter'
);

CREATE TRIGGER IF NOT EXISTS kb_docs_ai AFTER INSERT ON kb_docs BEGIN
  INSERT INTO kb_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS kb_docs_ad AFTER DELETE ON kb_docs BEGIN
  INSERT INTO kb_fts(kb_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS kb_docs_au AFTER UPDATE ON kb_docs BEGIN
  INSERT INTO kb_fts(kb_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO kb_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE INDEX IF NOT EXISTS idx_kb_docs_kind ON kb_docs(kind, created_at);

-- @migration 3
-- Persistent conversation memory (Phase 2): mention-chat turns scoped to
-- (guild_id, channel_id, user_id) - the same isolation key the in-memory
-- store always used. Additive only: no existing table, index or row changes.
-- Rows per scope are bounded on write (CONVERSATION_MAX_STORED_TURNS) and by
-- the daily retention sweep (CONVERSATION_RETENTION_DAYS).

CREATE TABLE IF NOT EXISTS conversation_turns (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id    TEXT NOT NULL,
  channel_id  TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_scope
  ON conversation_turns(guild_id, channel_id, user_id, id);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_created
  ON conversation_turns(created_at);

-- @migration 4
-- Knowledge-base isolation and review state. Every document is scoped to the
-- guild of the thread it came from; rows that cannot be resolved to a thread
-- keep an empty guild_id and are therefore invisible to guild-scoped retrieval.
-- Only moderator-approved solutions are approved; auto-indexed thread
-- fingerprints stay unapproved and never enter approved-only chat grounding.

ALTER TABLE kb_docs ADD COLUMN guild_id TEXT NOT NULL DEFAULT '';
ALTER TABLE kb_docs ADD COLUMN approved INTEGER NOT NULL DEFAULT 0;

UPDATE kb_docs
SET guild_id = CASE kind
  WHEN 'thread' THEN COALESCE(
    (SELECT guild_id FROM threads WHERE threads.id = kb_docs.ref_id), ''
  )
  WHEN 'solution' THEN COALESCE(
    (SELECT t.guild_id FROM solutions s JOIN threads t ON t.id = s.thread_id
     WHERE s.id = kb_docs.ref_id), ''
  )
  ELSE ''
END
WHERE guild_id = '';

UPDATE kb_docs SET approved = 1 WHERE kind = 'solution';

CREATE INDEX IF NOT EXISTS idx_kb_docs_guild ON kb_docs(guild_id);

-- @migration 5
-- Generic approval state machine for future public/privileged actions.
-- Only structural action metadata and a content hash are stored; action content
-- is never persisted or placed in audit metadata. Transitions are conditional
-- in the repository so concurrent decisions cannot overwrite each other.

CREATE TABLE IF NOT EXISTS approval_requests (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  action_type       TEXT NOT NULL,
  guild_id          TEXT NOT NULL,
  target_type       TEXT NOT NULL,
  target_id         TEXT NOT NULL,
  content_version   TEXT NOT NULL,
  content_hash      TEXT NOT NULL CHECK (length(content_hash) = 64),
  requested_by      TEXT NOT NULL,
  requester_kind    TEXT NOT NULL CHECK (requester_kind IN ('agent','human','system')),
  state             TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','approved','rejected','expired',
                                     'cancelled','executing','executed','failed')),
  created_at        TEXT NOT NULL,
  expires_at        TEXT NOT NULL,
  decided_by        TEXT,
  decided_at        TEXT,
  decision_note     TEXT,
  cancelled_by      TEXT,
  cancelled_at      TEXT,
  cancel_reason     TEXT
);

CREATE TABLE IF NOT EXISTS approval_executions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  approval_id       INTEGER NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,
  idempotency_key   TEXT NOT NULL,
  executor_id       TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN ('executing','executed','failed')),
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  error_code        TEXT
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_due
  ON approval_requests(state, expires_at);
CREATE INDEX IF NOT EXISTS idx_approval_requests_guild
  ON approval_requests(guild_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_executions_key
  ON approval_executions(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_executions_approval
  ON approval_executions(approval_id);

-- @migration 6
-- Phase 2B Content Studio: drafts and immutable content versions. Draft text is
-- stored here (it is community content, not a secret); approval rows still store
-- only a hash/binding. Publishing records the returned Discord message id on the
-- existing execution attempt so an ambiguous send can be reconciled without
-- re-sending. There is no schedule column: Phase 2B publishes immediately after
-- a separate human publish action.

CREATE TABLE IF NOT EXISTS content_drafts (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id           TEXT NOT NULL,
  target_channel_id  TEXT NOT NULL,
  content_type       TEXT NOT NULL
                     CHECK (content_type IN ('announcement','educational_post',
                                             'poll_proposal','event_announcement')),
  topic              TEXT NOT NULL,
  audience           TEXT NOT NULL DEFAULT '',
  tone               TEXT NOT NULL DEFAULT '',
  purpose            TEXT NOT NULL DEFAULT '',
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  current_version    INTEGER NOT NULL DEFAULT 1 CHECK (current_version >= 1),
  approval_id        INTEGER REFERENCES approval_requests(id) ON DELETE SET NULL,
  state              TEXT NOT NULL DEFAULT 'draft'
                     CHECK (state IN ('draft','submitted','published','failed'))
);

CREATE INDEX IF NOT EXISTS idx_content_drafts_guild
  ON content_drafts(guild_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_drafts_approval
  ON content_drafts(approval_id);

CREATE TABLE IF NOT EXISTS content_draft_versions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id           INTEGER NOT NULL REFERENCES content_drafts(id) ON DELETE CASCADE,
  version            INTEGER NOT NULL CHECK (version >= 1),
  content            TEXT NOT NULL CHECK (length(content) > 0),
  content_hash       TEXT NOT NULL CHECK (length(content_hash) = 64),
  validation_json    TEXT NOT NULL DEFAULT '[]',
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  UNIQUE (draft_id, version)
);

CREATE INDEX IF NOT EXISTS idx_content_draft_versions_hash
  ON content_draft_versions(content_hash);

ALTER TABLE approval_executions ADD COLUMN result_ref TEXT;

-- @migration 7
-- Phase 3 Living Knowledge Base: existing solutions remain the only candidate
-- lifecycle. Source metadata is additive and nullable so every pre-migration
-- solution keeps working as a manual candidate. An approved solution still
-- becomes searchable only through its existing approved kb_docs row; thread
-- fingerprints remain unapproved. No candidate or evidence table is added.

ALTER TABLE solutions ADD COLUMN content_hash TEXT;
ALTER TABLE solutions ADD COLUMN source_summary_id INTEGER
  REFERENCES summaries(id) ON DELETE SET NULL;
ALTER TABLE solutions ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'manual'
  CHECK (source_kind IN ('manual','summary','pulse'));

CREATE INDEX IF NOT EXISTS idx_solutions_thread
  ON solutions(thread_id, state);
CREATE INDEX IF NOT EXISTS idx_solutions_hash
  ON solutions(content_hash);

-- @migration 8
-- Missions, web research, content planning and offline evaluation. Additive
-- only. Every table carries guild_id and every repository read filters on it;
-- missions and their steps are deleted together by retention. Content drafts
-- gain provenance columns so a research- or pulse-backed draft can be traced
-- back to its source without trusting free-text claims.

CREATE TABLE IF NOT EXISTS missions (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id             TEXT NOT NULL,
  channel_id           TEXT NOT NULL,
  parent_channel_id    TEXT,
  requester_id         TEXT NOT NULL,
  goal                 TEXT NOT NULL,
  mode                 TEXT NOT NULL CHECK (mode IN ('community','research','draft_prep')),
  status               TEXT NOT NULL DEFAULT 'planned'
                       CHECK (status IN ('planned','running','completed','failed','cancelled')),
  plan_json            TEXT NOT NULL DEFAULT '[]',
  result_json          TEXT,
  error_code           TEXT,
  trace_id             TEXT NOT NULL,
  idempotency_key      TEXT,
  cancel_requested_at  TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  started_at           TEXT,
  finished_at          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_missions_idempotency
  ON missions(guild_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_missions_guild_created
  ON missions(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_guild_status
  ON missions(guild_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS mission_steps (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id       INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  step_index       INTEGER NOT NULL CHECK (step_index >= 0),
  title            TEXT NOT NULL,
  skill_id         TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','running','succeeded','failed','skipped','cancelled')),
  input_json       TEXT NOT NULL DEFAULT '{}',
  output_json      TEXT,
  references_json  TEXT NOT NULL DEFAULT '[]',
  error            TEXT,
  started_at       TEXT,
  finished_at      TEXT,
  UNIQUE (mission_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_mission_steps_mission
  ON mission_steps(mission_id, step_index);

CREATE TABLE IF NOT EXISTS research_reports (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id     TEXT NOT NULL,
  mission_id   INTEGER REFERENCES missions(id) ON DELETE SET NULL,
  query        TEXT NOT NULL,
  provider     TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('completed','unavailable','failed')),
  summary      TEXT NOT NULL DEFAULT '',
  synthesis    TEXT NOT NULL DEFAULT 'extractive'
               CHECK (synthesis IN ('model','extractive')),
  sources_json TEXT NOT NULL DEFAULT '[]',
  error_code   TEXT,
  created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_reports_guild
  ON research_reports(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_reports_mission
  ON research_reports(mission_id);

ALTER TABLE content_drafts ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE content_drafts ADD COLUMN source_ref TEXT;
CREATE INDEX IF NOT EXISTS idx_content_drafts_source
  ON content_drafts(guild_id, source_kind, source_ref);

CREATE TABLE IF NOT EXISTS event_plans (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id           TEXT NOT NULL,
  title              TEXT NOT NULL,
  brief              TEXT NOT NULL,
  scheduled_for      TEXT,
  target_channel_id  TEXT,
  plan_json          TEXT NOT NULL,
  state              TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','archived')),
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_plans_guild
  ON event_plans(guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS content_calendar_entries (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id           TEXT NOT NULL,
  title              TEXT NOT NULL,
  kind               TEXT NOT NULL CHECK (kind IN ('announcement','event','post','reminder')),
  starts_at          TEXT NOT NULL,
  ends_at            TEXT,
  target_channel_id  TEXT,
  draft_id           INTEGER REFERENCES content_drafts(id) ON DELETE SET NULL,
  event_plan_id      INTEGER REFERENCES event_plans(id) ON DELETE SET NULL,
  notes              TEXT NOT NULL DEFAULT '',
  state              TEXT NOT NULL DEFAULT 'planned' CHECK (state IN ('planned','cancelled','done')),
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calendar_guild_start
  ON content_calendar_entries(guild_id, starts_at);

CREATE TABLE IF NOT EXISTS contextual_replies (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id           TEXT NOT NULL,
  channel_id         TEXT NOT NULL,
  source_message_id  TEXT NOT NULL,
  source_excerpt     TEXT NOT NULL,
  tone               TEXT NOT NULL DEFAULT '',
  purpose            TEXT NOT NULL DEFAULT '',
  content            TEXT NOT NULL,
  state              TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','dismissed')),
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contextual_replies_guild
  ON contextual_replies(guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_eval_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id      TEXT NOT NULL,
  requested_by  TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('completed','failed')),
  report_json   TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_eval_runs_guild
  ON agent_eval_runs(guild_id, created_at DESC);

ALTER TABLE audit_log ADD COLUMN guild_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_audit_log_guild
  ON audit_log(guild_id, created_at DESC);

-- @migration 9
-- Gap closure: multimodal attachment metadata, guild voice profiles and
-- content feedback, AI moderation policies/incidents/warnings, host-agent run
-- traces, and durable scheduled jobs. Additive only. Every table is guild-
-- scoped; extracted attachment text is bounded and retention-swept. No public
-- publishing job kind exists: scheduled jobs may only run internal, read-only
-- or draft-preparation work.

CREATE TABLE IF NOT EXISTS attachments (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id       TEXT NOT NULL,
  channel_id     TEXT NOT NULL,
  user_id        TEXT NOT NULL,
  message_id     TEXT NOT NULL,
  filename       TEXT NOT NULL DEFAULT '',
  kind           TEXT NOT NULL
                 CHECK (kind IN ('image','pdf','document','text','code','log','unsupported')),
  mime           TEXT NOT NULL DEFAULT '',
  size_bytes     INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
  sha256         TEXT NOT NULL DEFAULT '',
  width          INTEGER,
  height         INTEGER,
  pages          INTEGER,
  extracted_text TEXT NOT NULL DEFAULT '',
  truncated      INTEGER NOT NULL DEFAULT 0,
  error_code     TEXT,
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_scope
  ON attachments(guild_id, channel_id, user_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_message
  ON attachments(guild_id, message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_created
  ON attachments(created_at);

CREATE TABLE IF NOT EXISTS guild_voice_profiles (
  guild_id           TEXT PRIMARY KEY,
  tone               TEXT NOT NULL DEFAULT '',
  audience           TEXT NOT NULL DEFAULT '',
  emoji_policy       TEXT NOT NULL DEFAULT 'sparing'
                     CHECK (emoji_policy IN ('none','sparing','allowed')),
  formatting         TEXT NOT NULL DEFAULT '',
  style_notes        TEXT NOT NULL DEFAULT '',
  banned_phrases_json TEXT NOT NULL DEFAULT '[]',
  updated_by         TEXT NOT NULL DEFAULT '',
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_feedback (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id    TEXT NOT NULL,
  draft_id    INTEGER REFERENCES content_drafts(id) ON DELETE SET NULL,
  version     INTEGER,
  feedback    TEXT NOT NULL,
  tags_json   TEXT NOT NULL DEFAULT '[]',
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_content_feedback_guild
  ON content_feedback(guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS moderation_policies (
  guild_id                 TEXT PRIMARY KEY,
  enabled                  INTEGER NOT NULL DEFAULT 0,
  report_channel_id        TEXT,
  delete_enabled           INTEGER NOT NULL DEFAULT 0,
  severity_threshold       TEXT NOT NULL DEFAULT 'high'
                           CHECK (severity_threshold IN ('low','medium','high','critical')),
  confidence_threshold     REAL NOT NULL DEFAULT 0.75
                           CHECK (confidence_threshold >= 0 AND confidence_threshold <= 1),
  categories_json          TEXT NOT NULL DEFAULT '[]',
  max_incidents_per_day    INTEGER NOT NULL DEFAULT 50 CHECK (max_incidents_per_day >= 1),
  notify_user              INTEGER NOT NULL DEFAULT 0,
  created_by               TEXT NOT NULL DEFAULT '',
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation_incidents (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id                TEXT NOT NULL,
  message_id              TEXT NOT NULL,
  channel_id              TEXT NOT NULL,
  author_id               TEXT NOT NULL,
  category                TEXT NOT NULL,
  severity                TEXT NOT NULL
                          CHECK (severity IN ('none','low','medium','high','critical')),
  confidence              REAL NOT NULL
                          CHECK (confidence >= 0 AND confidence <= 1),
  action_taken            TEXT NOT NULL
                          CHECK (action_taken IN ('none','flagged','deleted','warned','reported')),
  state                   TEXT NOT NULL DEFAULT 'open'
                          CHECK (state IN ('open','confirmed','dismissed','overridden')),
  repeat_count            INTEGER NOT NULL DEFAULT 0 CHECK (repeat_count >= 0),
  report_delivered        INTEGER NOT NULL DEFAULT 0,
  recommended_next_step   TEXT NOT NULL DEFAULT '',
  reviewer_id             TEXT,
  review_note             TEXT,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL,
  UNIQUE (guild_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_moderation_incidents_guild
  ON moderation_incidents(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_moderation_incidents_author
  ON moderation_incidents(guild_id, author_id, created_at DESC);

CREATE TABLE IF NOT EXISTS moderation_warnings (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id     TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  incident_id  INTEGER NOT NULL REFERENCES moderation_incidents(id) ON DELETE CASCADE,
  category     TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE (guild_id, incident_id)
);

CREATE INDEX IF NOT EXISTS idx_moderation_warnings_user
  ON moderation_warnings(guild_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id       TEXT NOT NULL,
  channel_id     TEXT NOT NULL,
  parent_channel_id TEXT,
  requester_id   TEXT NOT NULL,
  goal           TEXT NOT NULL,
  mode           TEXT NOT NULL
                 CHECK (mode IN ('community','research','draft_prep','moderation','triage')),
  status         TEXT NOT NULL DEFAULT 'planned'
                 CHECK (status IN ('planned','running','completed','partial','failed','cancelled')),
  plan_json      TEXT NOT NULL DEFAULT '[]',
  result_json    TEXT,
  error_code     TEXT,
  trace_id       TEXT NOT NULL,
  idempotency_key TEXT,
  cancel_requested_at TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  started_at     TEXT,
  finished_at    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_idempotency
  ON agent_runs(guild_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_runs_guild
  ON agent_runs(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status
  ON agent_runs(guild_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_run_steps (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  step_index       INTEGER NOT NULL CHECK (step_index >= 0),
  role_id          TEXT NOT NULL,
  title            TEXT NOT NULL,
  skill_id         TEXT NOT NULL,
  depends_on_json  TEXT NOT NULL DEFAULT '[]',
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','running','succeeded','failed','skipped','cancelled')),
  input_json       TEXT NOT NULL DEFAULT '{}',
  output_json      TEXT,
  references_json  TEXT NOT NULL DEFAULT '[]',
  error            TEXT,
  started_at       TEXT,
  finished_at      TEXT,
  UNIQUE (run_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_agent_run_steps_run
  ON agent_run_steps(run_id, step_index);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id         TEXT NOT NULL,
  job_kind         TEXT NOT NULL
                   CHECK (job_kind IN ('agent.run','content.draft','digest.collect')),
  payload_json     TEXT NOT NULL DEFAULT '{}',
  run_at           TEXT NOT NULL,
  timezone         TEXT NOT NULL DEFAULT 'UTC',
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
  attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts     INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  last_error       TEXT,
  idempotency_key  TEXT NOT NULL,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  started_at       TEXT,
  finished_at      TEXT,
  UNIQUE (guild_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
  ON scheduled_jobs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_guild
  ON scheduled_jobs(guild_id, created_at DESC);

ALTER TABLE content_calendar_entries ADD COLUMN scheduled_job_id INTEGER
  REFERENCES scheduled_jobs(id) ON DELETE SET NULL;

-- Chat replies that require explicit human approval. The exact reply text and
-- destination are stored so a moderator approves precisely what will be sent;
-- there is no edit path after creation. A new version requires a new row.
CREATE TABLE IF NOT EXISTS reply_approvals (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id             TEXT NOT NULL,
  channel_id           TEXT NOT NULL,
  reply_to_message_id  TEXT,
  content              TEXT NOT NULL CHECK (length(content) > 0),
  content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
  state                TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending','approved','rejected','expired','sent','failed')),
  requested_by         TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  expires_at           TEXT NOT NULL,
  decided_by           TEXT,
  decided_at           TEXT,
  decision_note        TEXT,
  sent_message_id      TEXT,
  error_code           TEXT,
  updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reply_approvals_guild
  ON reply_approvals(guild_id, state, created_at DESC);

-- @migration 10
-- Widen the agent_runs.mode CHECK to include the read-only evaluation host
-- mode. SQLite cannot alter a CHECK constraint in place, so both agent tables
-- are rebuilt. The child rows are staged first and restored after the parent
-- so the ON DELETE CASCADE cannot destroy existing run steps. Explicit ids
-- keep every reference stable, and the indexes are recreated below.

CREATE TABLE agent_runs_new (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id       TEXT NOT NULL,
  channel_id     TEXT NOT NULL,
  parent_channel_id TEXT,
  requester_id   TEXT NOT NULL,
  goal           TEXT NOT NULL,
  mode           TEXT NOT NULL
                 CHECK (mode IN ('community','research','draft_prep','moderation','triage','evaluation')),
  status         TEXT NOT NULL DEFAULT 'planned'
                 CHECK (status IN ('planned','running','completed','partial','failed','cancelled')),
  plan_json      TEXT NOT NULL DEFAULT '[]',
  result_json    TEXT,
  error_code     TEXT,
  trace_id       TEXT NOT NULL,
  idempotency_key TEXT,
  cancel_requested_at TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  started_at     TEXT,
  finished_at    TEXT
);

INSERT INTO agent_runs_new (
  id, guild_id, channel_id, parent_channel_id, requester_id, goal, mode, status,
  plan_json, result_json, error_code, trace_id, idempotency_key,
  cancel_requested_at, created_at, updated_at, started_at, finished_at
)
SELECT
  id, guild_id, channel_id, parent_channel_id, requester_id, goal, mode, status,
  plan_json, result_json, error_code, trace_id, idempotency_key,
  cancel_requested_at, created_at, updated_at, started_at, finished_at
FROM agent_runs;

CREATE TABLE agent_run_steps_backup AS SELECT * FROM agent_run_steps;
DROP TABLE agent_run_steps;
DROP TABLE agent_runs;
ALTER TABLE agent_runs_new RENAME TO agent_runs;

CREATE UNIQUE INDEX idx_agent_runs_idempotency
  ON agent_runs(guild_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_agent_runs_guild
  ON agent_runs(guild_id, created_at DESC);
CREATE INDEX idx_agent_runs_status
  ON agent_runs(guild_id, status, updated_at DESC);

CREATE TABLE agent_run_steps (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  step_index       INTEGER NOT NULL CHECK (step_index >= 0),
  role_id          TEXT NOT NULL,
  title            TEXT NOT NULL,
  skill_id         TEXT NOT NULL,
  depends_on_json  TEXT NOT NULL DEFAULT '[]',
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','running','succeeded','failed','skipped','cancelled')),
  input_json       TEXT NOT NULL DEFAULT '{}',
  output_json      TEXT,
  references_json  TEXT NOT NULL DEFAULT '[]',
  error            TEXT,
  started_at       TEXT,
  finished_at      TEXT,
  UNIQUE (run_id, step_index)
);

INSERT INTO agent_run_steps (
  id, run_id, step_index, role_id, title, skill_id, depends_on_json, status,
  input_json, output_json, references_json, error, started_at, finished_at
)
SELECT
  id, run_id, step_index, role_id, title, skill_id, depends_on_json, status,
  input_json, output_json, references_json, error, started_at, finished_at
FROM agent_run_steps_backup;

DROP TABLE agent_run_steps_backup;

CREATE INDEX idx_agent_run_steps_run
  ON agent_run_steps(run_id, step_index);
