# Audit records — contents and retention

Community Copilot keeps a small append-only `audit_log` table in the same local
SQLite database. Audit rows are operational evidence for actions that a human
reviewer may need to reconstruct later (approvals, publications, purges, and
moderator decisions).

## What is stored

| Column | Contents |
|---|---|
| `created_at` | UTC ISO-8601 timestamp |
| `actor_id` | Discord user id, or `system` for automatic transitions |
| `action` | Stable event name (examples below) |
| `target_type`, `target_id` | Internal resource kind and id (thread, solution, approval, draft, `kb_doc`) |
| `meta_json` | Small structured metadata: ids, counts, states, hashes, error/reason codes |

Representative events:

| Action | Metadata |
|---|---|
| `solution.proposed` / `solution.approved` / `solution.rejected` | thread/solution ids, state; rejection stores only whether/how long a moderator note was (the note itself stays on the solution row) |
| `knowledge.candidate.proposed` / `knowledge.candidate.approved` / `...rejected` | guild id, thread id, source kind/summary id, content SHA-256, finding codes |
| `knowledge.document_purged` | guild id, document id, kind, source ref id, body character count |
| `approval.requested` / `approval.*` | action type, guild, target, content version + SHA-256, state, reason code; never action content |
| `content.*` | draft/approval/execution ids, message id, error code, content version; never draft text |
| `thread.forgotten` / `conversation.cleared` / `status.corrected` | counts, status, target ids; a correction note stays on the corrections row, not in audit |

The audit never contains message bodies, thread text, candidate text, draft
text, titles, URLs, tokens, or API keys. Discord user ids and internal row ids
are retained because they are the minimum needed to attribute a decision or a
deletion; names and other profile data are not stored.

## Retention policy

- **Audit rows are append-only and retained indefinitely by the application.**
  No automatic sweep deletes them.
- `RETENTION_DAYS`, the conversation-turn sweep, `/forget`, and approved-KB
  document purge do **not** delete audit rows. Deleting the evidence of a purge
  would undermine the purpose of the audit.
- Approval/audit rows survive expiry and cancellation; expiry changes state
  without deleting history.
- There is intentionally no `/audit-delete` command and no
  `AUDIT_RETENTION_DAYS` setting. Adding one would be a separate, explicit
  privacy/operations decision requiring tests and documentation.
- An operator who must remove audit history for a legal/privacy request should
  do so deliberately at the local database layer, after a verified backup, and
  outside the running bot. This is not automated and is not exposed in Discord.

## Access and integrity

- The database is local-only and gitignored; it is never attached to Discord.
- The bot exposes no command that reads or edits arbitrary audit rows.
- Audit writes for state-changing workflows share the same SQLite transaction
  as the state change, so an audit failure rolls the action back (for example,
  a failed KB purge leaves the document and the FTS index intact).
- Audit metadata is built from trusted application values; model output is
  never copied into audit records.
