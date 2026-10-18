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
| T5 | Unauthorized command use in wrong guild | Allowlists + moderator permission checks in code | Misconfigured allowlist (operator error) — mitigated by safe default (empty = deny) |
| T6 | Sensitive content persisted indefinitely | Retention sweep, `/forget` full purge incl. FTS, no body logging | Raw Discord history itself is outside bot control |
| T7 | DB leaked/shared | DB is gitignored, local-only, never attached to Discord; docs recommend disk encryption | Backups made by the user are the user's responsibility |
| T8 | Misleading AI status harms moderation | Status shown as suggestion + confidence; moderator override stored; no automatic moderation actions | Small-model quality limits; documented in AI_SYSTEM.md |
| T9 | Untrusted retrieved KB content re-injects | KB text fenced like all content; only approved solutions are authoritative | Garbage-in from previously approved bad advice — approval workflow is the gate |
| T10 | Link in message leads somewhere malicious when a human clicks | Bot never fetches/executes links; displays them as plain text only | Human users click links themselves (out of bot's control) |

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
