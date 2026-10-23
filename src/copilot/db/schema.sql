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
