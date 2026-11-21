# Privacy Policy — Webbee

**Effective date:** 2026-11-21
**Published by:** Imperal, Inc. — [imperal.io](https://imperal.io)

---

## What Webbee is

`webbee` is an open-source terminal coding agent (`GPL-3.0-or-later`) that runs on your machine and connects to your [Imperal Cloud](https://imperal.io) account via the open [ICNLI](https://icnli.org) protocol. Local tools (file reads/edits, shell execution, tree-sitter code indexing, and Shadow Git checkpoints) run directly inside your workspace; reasoning, cross-surface session memory, and fact-checked planning run through your Imperal Cloud account.

---

## Data flows

| What | Where it goes | When |
|------|--------------|------|
| Your Imperal credentials | Stored locally in `~/.imperal/credentials.json` (mode `0600`, or the path set in `IMPERAL_CREDENTIALS_FILE`) | On `webbee login` or `/login` |
| Local logs | Stored locally in `~/.cache/webbee/webbee.log` (rotated at 2 MB) | During normal operation |
| Shadow Git checkpoints | A local shadow store on your machine, outside your repository's `.git` history | Automatically before local writes or on `checkpoint` |
| Your prompts, tool calls, tool outputs, and code context sent during a turn | Your private space in Imperal Cloud (and the selected model tier for that turn) | While you run an interactive session, `--marathon`, or `--once` turn |
| Code index metadata (symbol names, signatures, line ranges, call graph, routes, file hashes) | Local index cache (`IMPERAL_INTEL_CACHE_DIR` or default cache dir) and synced to your private Imperal Cloud code map | On workspace boot and after edits (can be disabled locally via `IMPERAL_INTEL=false`) |
| Session history & memory across surfaces | Your private space in Imperal Cloud | Shared across `webbee`, `imperal-mcp`, the web panel, and Telegram |
| Login | Browser OAuth 2.0 Device Authorization Grant (RFC 8628) + PKCE -> Imperal gateway | No password ever passes through `webbee` |

All network requests go to your Imperal Cloud endpoint at `https://auth.imperal.io` (or `IMPERAL_API_URL`) over HTTPS.

---

## Scrubbed before it reaches the model

Personal details (secrets, private keys, tokens, JWTs, API keys, authorization headers, credentials in URLs, emails, phone numbers, payment card numbers, and SSNs) are masked before they reach the model. In the terminal Home view, your account email is also masked on screen.

---

## What Webbee does NOT do

- **No hidden analytics or ad telemetry:** Webbee does not embed third-party tracking SDKs, and suppresses HuggingFace download telemetry (`HF_HUB_DISABLE_TELEMETRY=1`) at startup.
- **No silent destructive actions:** Risky commands, rollbacks, and destructive cloud tools ask for confirmation in `default` mode; money/billing actions require explicit approval in the Imperal panel.
- **No silent `.git` rewriting:** Shadow Git checkpoints live outside your repository's `.git` directory.
- **No cross-agent privilege escalation:** Messages from other live sessions inform the agent; they never approve actions on your behalf.

---

## Your control

- **Inspect & revoke sessions:** Run `/sessions` to view active sessions across devices, `/logout-others` to revoke other sessions, or `webbee logout` (`/logout`) to revoke the current sign-in and delete local credentials.
- **Manage or delete cloud memory:** View or delete stored session history and memory anytime from [panel.imperal.io](https://panel.imperal.io). Deleting your account deletes your private space with it.
- **Disable local indexing:** Set `IMPERAL_INTEL=false` to turn off the on-disk code index and file watcher.

---

## Platform security & full policy

Imperal's platform privacy policy governs data stored in Imperal Cloud: [imperal.io/privacy](https://imperal.io/privacy).
Security architecture and audit documentation: [docs.imperal.io/en/guides/audit-and-security](https://docs.imperal.io/en/guides/audit-and-security/).

---

## Contact

Privacy questions or data requests: [support@imperal.io](mailto:support@imperal.io)
Security reports: [security@imperal.io](mailto:security@imperal.io) (see [SECURITY.md](SECURITY.md))
