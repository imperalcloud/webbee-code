# Changelog

## 0.2.0

- **Marathon is now the default.** A task you type runs to completion on its own
  — it plans, edits, runs, and verifies across as many steps as it needs, and
  only stops when the goal is done, you stop it, or your credits run low (it
  pauses, it doesn't fail — top up and it resumes). No more fixed per-run step
  limit on the default path.
- **`--once` (alias `--no-marathon`)** runs a single bounded coding turn instead,
  for a quick one-shot.

## 0.1.14

- **Esc/Ctrl-C now actually stops the turn — server-side, not just locally.**
  Previously Ctrl-C only cancelled the LOCAL asyncio task; the cloud brain kept
  running the turn server-side regardless (burning tokens/credits and still
  landing writes) — Esc did nothing while a turn was running. Both keys now
  also post a cancel to the gateway for the in-flight session (fail-soft: a
  network hiccup here never blocks the local teardown). The busy toolbar hint
  now reads "Esc/Ctrl-C to stop".

## 0.1.13

- **Fixes lag in long sessions.** The scrollable output pane re-read and
  re-scanned the ENTIRE transcript (a full-buffer copy + compare) on every
  redraw — so in a big session every keystroke, spinner tick and scroll cost
  O(session). It now keys its line cache on the stream write position and
  re-reads only when new output actually arrives, so redraws are O(viewport).

## 0.1.12

- **Fixes the dock freezing / lagging.** Building the per-turn coding context
  (`git status` + a file-tree walk) and reading the git branch ran
  synchronously on the UI event loop, freezing the whole terminal at every turn
  start (and on keystrokes that triggered the branch read) — worst on large or
  slow repos. Both now run on a worker thread, so the dock stays responsive.

## 0.1.11

- Pin `imperal-sdk>=5.9.3` (resolve the current SDK).

## 0.1.10

- **Terminal shows credits + tokens, not raw dollars.** The status toolbar and
  `/cost` / `/status` report the session's credits + token count; the
  underlying LLM dollar cost stays server-side.

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
