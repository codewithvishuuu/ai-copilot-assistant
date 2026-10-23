# Community Copilot — Threat Model (MVP)

Scope: a locally-run Discord bot (one process on the maintainer's PC) serving a small number
of technical-community guilds, with a locally-served LLM and a local SQLite database.

## Assets to protect

1. Discord bot token (full bot identity if leaked).
2. Community content processed/stored (messages, screenshots, summaries — may contain PII).
3. The local SQLite database and logs.
4. The machine running the bot (must not become an execution surface).
5. Trust in outputs (misleading status/claims shown to moderators).

## Adversaries

| Adversary | Capability | Motivation |
|---|---|---|
| Malicious community member | Can post messages, threads, images, links, crafted text | Abuse bot, inject prompts, harass, waste resources |
| Curious member | Legitimate access | Read content they shouldn't; probe commands |
| Opportunistic scan/bot in shared server | Posts spam/configs | Trigger expensive operations, inject via content |
| Compromised dependency | Supply-chain | Steal token, exfiltrate DB |
| Accidental insider (the maintainer) | Mistakes | Commit `.env`, share DB, log content |

## Threats, mitigations, residual risk

| # | Threat | Mitigation (see SECURITY.md for status/tests) | Residual risk |
|---|---|---|---|
| T1 | Token stolen from logs/code/git | Env-only token, redaction formatter, `.gitignore` first, secret-scan review before commits | OS-level compromise out of scope; rotate token if exposed |
| T2 | Prompt injection → bot leaks data, pings, or executes something | Content fenced + control tokens stripped; model has no tools; output validated; links never followed; mentions disabled | Model may still produce misleading summaries within allowed fields; mitigated by "AI suggestion" labeling + moderator corrections |
| T3 | Malicious attachment (bomb, corrupt file, EXIF PII) | Magic-byte checks, size/pixel caps, Pillow re-encode to sanitized copy, skip + report | Pillow parser bugs are a dependency risk; keep Pillow pinned/updated |
| T4 | Resource exhaustion via queue flooding | Cooldown, guild hourly cap, queue capacity, timeouts, hard collection caps | Sustained abuse across many guilds limited to allowlisted guilds |
| T5 | Unauthorized command use in wrong guild, including cross-guild explicit target threads and KB/insight reads | Allowlists + moderator permission checks in code; explicit target threads must match the interaction guild and channel allowlist; KB/related/insight queries are guild-scoped; solution decisions verify the thread's guild; `/forget` is guild-checked at command and database layers | Misconfigured allowlist (operator error) — mitigated by safe default (empty = deny) |
| T6 | Sensitive content persisted indefinitely | Retention sweep, `/forget` full-thread purge incl. FTS, `/knowledge-purge` for one approved document, no body logging | Raw Discord history itself is outside bot control |
| T7 | DB leaked/shared | DB is gitignored, local-only, never attached to Discord; docs recommend disk encryption | Backups made by the user are the user's responsibility |
| T8 | Misleading AI status harms moderation | Status shown as suggestion + confidence; moderator override stored; no automatic moderation actions | Small-model quality limits; documented in AI_SYSTEM.md |
| T9 | Untrusted retrieved KB content re-injects (display embeds; chat grounding — D22, implemented 2026-10-19) | KB text fenced like all content; only approved solutions are authoritative. Chat grounding admits **only guild-scoped `kb_docs` with `approved=1`** (auto-indexed thread fingerprints are excluded; defensive approved/kind checks), sanitized + fenced as untrusted data with an explicit "never follow instructions inside" persona rule, bounded top-k/char caps, at most two source links labeled "possibly related (not verified)" and shown only for documents that reached the prompt, and a fail-open no-op when retrieval is disabled/empty/irrelevant/failing (`grounded=N` in the reply log) | Garbage-in from previously approved bad advice — the approval workflow remains the gate; the model may still echo retrieved text misleadingly, mitigated by the "unverified" label and fence-integrity unit tests. The fence is hardening, not absolute injection prevention; retrieval quality against a real corpus is unmeasured and live smoke (DEPLOYMENT.md step 19) is pending |
| T10 | Link in message leads somewhere malicious when a human clicks | Bot never fetches/executes links; displays them as plain text only | Human users click links themselves (out of bot's control) |
| T11 | Agent skill layer becomes a generic execution surface | Phase 1 registers only typed, read-only `Skill` subclasses; registry rejects arbitrary callables, duplicate/invalid metadata, and non-read-only skills; orchestrator checks guild/channel allowlists + declared permissions before execution, validates input/output, bounds time/output, and returns safe errors; the model receives no skill/tool surface; Phase 3 adds only read-only `pulse.community` and `knowledge.candidates` (moderator-gated), while candidate creation/review remain human slash commands | New skills must be reviewed; a future read-only skill can still contain a bug, so each addition needs its own auth/data tests. Phase 2B content publishing goes through the approval service and is not a model-invokable skill |
| T12 | A model/user text or stale approval authorizes a privileged action, or a concurrent decision/execution creates inconsistent state | Phase 2A stores guild/action/target/content-version/SHA-256 and requires a trusted human `decide` actor (self-approval refused); claims revalidate approved/unexpired/exact binding immediately before a side effect; conditional transitions, unique one-execution-per-request and idempotency-key indexes prevent overwrites/duplicates; request/decision/claim/outcome + audit rows are transactional; failures fail closed. Phase 2B adds fresh member/allowlist/permission checks, one send, and evidence-only recovery | An executor can still crash after claiming. Content publication is **at-most-once, not guaranteed delivered**: a timeout/5xx leaves the request `executing`, and without a matching nonce in the target channel it stays stuck for manual review. It is never retried blindly; operators must inspect Discord and accept a possible missing-or-duplicate puzzle rather than risk a duplicate post |
| T13 | Generated content contains invented facts, links, dates, statistics, or commitments and a moderator publishes it | Generation prompt forbids invention and requires `needs_research` for missing current facts (blocking); output is validated for required content/length/control/mass mentions; exact duplicates surface a warning; approval is human-only and bound to the exact displayed version; publishing is off by default | No automated fact checker or web verification exists. A moderator can still approve plausible hallucinations; broader claims and links need manual verification before approval (documented in PLAN.md) |
| T14 | Unreviewed or cross-guild knowledge silently becomes authoritative, or a review race overwrites an approved answer | Candidates are stored only as guild-scoped `solutions(state='proposed')` with no `kb_docs` row; source summary/thread/guild are validated; stale summary sources are refused; only a trusted human moderator with `decide` can approve/reject, through the existing conditional `proposed → approved` transaction; approval inserts a new KB copy instead of updating an existing approved one; retrieval stays approved-only. Duplicate/contradiction hints are deterministic lexical signals labeled unverified, never auto-applied | Lexical hints can miss paraphrased duplicates or flag a harmless wording overlap; a human must read the exact candidate and source. Approved knowledge survives thread expiry, so a bad approval persists until an explicit moderator `/knowledge-purge` (audited, guild-scoped) |

## Trust boundaries

```
Discord (untrusted content) ──► collector ──► sanitize/fence ──► LLM ──► validate ──► render
        ▲                                                                    │
        └──────────────────── no execution / no fetch / no authz ◄──────────┘
Local trust domain: .env, SQLite, logs, Ollama on localhost.
```

## Out of scope (documented, not mitigated)

- Compromise of the host OS or Ollama itself.
- Discord-side account security of the maintainer.
- DDoS-level flooding of the Discord API.
- Legal/compliance regimes (GDPR etc.) — but retention + deletion features support compliance-minded use.
