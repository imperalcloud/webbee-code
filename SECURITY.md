# Security Policy — Webbee

We take the security of Webbee and Imperal Cloud seriously.

## Supported Versions

Only the latest release of `webbee` is actively supported with security fixes. Webbee checks for updates on start and tracks the `stable` release channel by default.

| Version | Supported |
| ------- | --------- |
| Latest (`0.7.x`) | Yes |
| Older releases | No — please upgrade via `pipx upgrade --include-injected webbee` or `uv tool upgrade webbee` |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, email us privately at **[security@imperal.io](mailto:security@imperal.io)** (or [support@imperal.io](mailto:support@imperal.io)) with:

- A clear description of the vulnerability and its potential impact.
- Step-by-step reproduction instructions or a minimal proof of concept.
- The `webbee --version` output, OS, and Python version.

We aim to acknowledge reports within **48 hours** and provide a remediation timeline once reproduced.

## Security Architecture Highlights

- **Workspace confinement:** Local file tools resolve paths and refuse edits outside the active workspace root unless explicitly approved.
- **Approval gates:** Risky shell actions, destructive cloud tools, and rollback/external actions ask for confirmation before running (and billing/money actions require panel approval).
- **No password handling:** Authentication uses OAuth 2.0 Device Authorization Grant (RFC 8628) + PKCE over HTTPS; tokens are stored locally with `0600` permissions (`~/.imperal/credentials.json`).
- **PII masking:** Personal details (secrets, tokens, emails, phone numbers, card numbers) are masked locally before data is sent to the model.
- **Shadow Git isolation:** Checkpoints live outside your repository's `.git` history so rollbacks never rewrite your commits.

For full data-flow details, see [PRIVACY.md](PRIVACY.md) and [docs.imperal.io/en/guides/audit-and-security](https://docs.imperal.io/en/guides/audit-and-security/).
