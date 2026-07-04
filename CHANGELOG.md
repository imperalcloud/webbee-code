# Changelog

## 0.1.4

- **Copy on select.** Drag to select text in the dock and it's copied to your
  clipboard automatically (via OSC 52 — works locally and over SSH), with a
  brief “✓ copied” confirmation. Mouse-wheel scrolling still works.

## 0.1.3

- **Sessions & security.** `/sessions` lists everywhere your Imperal account is
  signed in — terminal (webbee), API, and web — with the current terminal
  marked. `/sessions revoke <#>` signs out one; `/logout-others` signs out
  every session except this one. Manage the same sessions from the panel
  (Settings → Security). Backed by a single gateway-owned session store.

## 0.1.2

- **Device-code login (RFC 8628).** `/login` and `webbee login` now use the
  device-authorization flow (via `imperal-mcp` 0.5.0): the terminal shows a
  short code + `https://panel.imperal.io/device`; you approve in any browser
  (even a phone), and the terminal polls until it's signed in. Works
  identically on a local machine, over SSH, in WSL, or headless — no
  `127.0.0.1` callback that a remote browser can never reach. Replaces the
  loopback browser-login and the 0.1.1 executor workaround.
- The dock renders the sign-in code + URL into the action feed (a bare print is
  invisible in the full-screen UI).
- Fix `__version__` so it tracks `pyproject.toml` (was pinned at 0.1.0).

## 0.1.1

- Fix `/login` inside the REPL: it now runs the shared `imperal_mcp` auth flow
  off the event loop, so the browser sign-in completes instead of failing with
  "asyncio.run() cannot be called from a running event loop". (`webbee login`
  from the shell already worked; this fixes the in-REPL command too — one auth
  mechanism for every surface.)

## 0.1.0

First public release.

- `webbee` — a coding agent in your terminal: reads, writes, and runs code in
  the current directory; the brain runs in the Imperal Cloud on ICNLI. No model
  keys on the machine.
- Full-screen dock: a scrollable, colored output pane with the input box and
  toolbar pinned at the bottom.
- Consent modes — **default** (asks before anything it can't undo), **plan**
  (read-only), **autopilot** (acts without asking); cycle with **Shift + TAB**.
  Spending money always needs a browser approval.
- Reaches your connected Imperal apps (mail, notes, tasks, …) alongside the
  local code tools.
- Slash commands: `/login` `/logout` `/mode` `/cost` `/status` `/clear` `/exit`.
- Live token/cost meter (session total) and update check.
