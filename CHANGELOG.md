# Changelog

## 0.1.9

- **Copy-on-select actually copies.** Drag-to-select now writes to the real
  local clipboard first (`pbcopy` on macOS, `xclip`/`wl-copy` on Linux) — OSC
  52 is only a fallback (useful over SSH). The confirmation is now honest: it
  only says "copied" once a local tool actually succeeded; otherwise it says
  the reply was sent via OSC 52, or that the copy failed. Previously it always
  flashed "✓ copied" via OSC 52 alone, which most terminals (Terminal.app
  entirely, iTerm2 without a permission toggle) silently ignore.
- **`/steps` — see what the last turn actually did.** Lists every tool/action
  from the last turn with a ✓/✗ mark; `/steps N` (or Up/Down + Enter in the
  dock, when the input is empty and idle) fetches and shows the full detail
  for one step — args/result previews, duration, trace id — from the gateway.

## 0.1.8

- **More robust file tools.** The local file tools now find the path under any
  reasonable argument name (and any key that mentions path/file), parse args
  delivered as a JSON string, and — if a path really is missing — report which
  keys the model DID send instead of a bare error. Pairs with a server-side fix
  that stops large file writes from being truncated.

## 0.1.7

- **Fix: editing files failed with `KeyError: 'path'`.** The local file tools now
  accept the Claude-Code argument names the model naturally emits
  (`file_path`, `old_string`, `new_string`, `content`) in addition to the short
  ones, and a genuinely missing path returns a clear message instead of crashing
  the tool. `write_file` / `edit_file` work reliably again.

## 0.1.6

- **Input height is truly dynamic.** The box is one line and grows only as far
  as your (wrapped) text needs — up to 10 rows — then shrinks back, instead of a
  fixed tall block.
- **Selection is visible.** Dragging highlights the text as you go (and copies
  it on release); the highlight clears when you let go.

## 0.1.5

- **No lag on large sessions + never-truncated answers.** The output pane now
  renders only the visible slice (virtualized), so scrolling and streaming stay
  instant no matter how long the session grows, and a long answer is shown in
  full — scroll to read all of it.
- **Input wraps.** Long input wraps and the box grows (up to 10 rows) so
  everything you type stays visible instead of scrolling off the side.

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
