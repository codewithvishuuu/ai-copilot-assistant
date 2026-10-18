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
