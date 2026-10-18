# RAG.md — Knowledge Base & Retrieval Design

**"RAG" here means retrieval for *display*, not retrieval for *prompting*.** Approved
solutions and thread fingerprints are retrieved from SQLite FTS5 and shown in Discord embeds
with source links. Retrieved text is **never fed back into the model** — there is no
retrieval→prompt loop to poison, which removes a whole class of indirect-injection risk
(see [THREAT_MODEL.md](THREAT_MODEL.md)).

## Why lexical (BM25) instead of embeddings

| Consideration | Decision |
|---|---|
| Hardware | Target machine: 8 GB RAM, CPU-only. A second embedding model + vector store would compete with `gemma4:e2b` for the same RAM. |
| Determinism | FTS5/BM25 ranking is reproducible and debuggable (`bm25()` scores in tests, `snippet()` markers). |
| Honesty | Lexical recall is lower for paraphrases — so every result is labeled **"possibly related (not verified)"**, never "duplicate". |
| Cost | Zero extra dependencies: FTS5 ships with CPython's SQLite (verified: `sqlite 3.45.1`, FTS5 OK). |

Known limitation, stated plainly: a re-worded duplicate ("upload broken" vs "file transfer
fails") can be missed. The UI never claims certainty it doesn't have. Semantic/hybrid search
is explicitly **out of scope** (a documented future option, not a hidden promise).

## Storage model

Migration 2 in `src/copilot/db/schema.sql` adds:

- `kb_docs` — the knowledge base. A row is either
  - `kind='solution'` — a moderator-approved resolution (title, body, optional source URL,
    link back to the Discord thread it came from), or
  - `kind='thread'` — a compact **fingerprint** of a summarized thread, built by
    `core/duplicate.py::fingerprint_text()` (overview + main problem + first three error
    signatures — not raw messages).
- `kb_fts` — an FTS5 external-content table over `kb_docs`, with **sync triggers** for
  insert/update/delete, so the index can never drift from the table (asserted in
  `tests/unit/test_repository.py`).

## Writer path: proposals → approval → index

1. A moderator runs `/resolve` on a thread → a **proposal** row is written to the `solutions`
   table (pending state) and an `audit_log` entry is recorded. Nothing is searchable yet.
2. `/approve-solution` promotes the proposal into `kb_docs` as `kind='solution'` (the FTS
   triggers index it immediately) — or `/reject`/reject path discards it; both paths are
   audited. Approval is the only way text enters the knowledge base.
3. Every stored summary optionally indexes a `kind='thread'` fingerprint, so future `/summarize`
   runs can surface related history.

These transitions are covered end-to-end in `tests/unit/test_solutions.py`.

## Read paths

| Path | Implementation | Behavior |
|---|---|---|
| `/kb-search <query>` | `db/repository.py::search_kb` via `ui/embeds.py::build_kb_search_embeds` | BM25-ranked (`bm25()` ascending — lower is better), titles + highlighted `snippet()` excerpts, source link when available. Query text is sanitized into quoted FTS terms (`util/text.py::build_fts_query`) so FTS syntax in user input can't break or game the query. |
| Related items on `/summarize` | `core/duplicate.py::find_related` → `repo.related_docs` | Top-k (default 3) documents excluding the thread being summarized; rendered under "possibly related (not verified)" with the kind (`solution`/`thread`) visible. |
| Snippets | FTS5 `snippet()` over the **body** column | Tests pin this down: matching terms must appear in `body` for `[ ]` highlight markers — a guarantee of where the excerpt comes from. |

## Deletion & retention (privacy)

- `RETENTION_DAYS` sweep and `/forget` purge a thread's messages, summaries and fingerprints
  **including their FTS rows** (external-content FTS + triggers handle the index; the purge is
  asserted after the fact in `tests/unit/test_retention.py`).
- Because retrieval is display-only, there is no cached embedding store anywhere to forget.

## Quality claims (what is actually verified)

- Ranking order, snippet sourcing, trigger-based index sync, parameterized SQL
  (injection-survivability), and the propose→approve→searchable lifecycle are all unit-tested
  (213-test suite; see [TESTING.md](TESTING.md)).
- Retrieval **quality against a real corpus** has not been measured yet — that is an honest
  open item, tracked in [PLAN.md](PLAN.md) known limitations.
