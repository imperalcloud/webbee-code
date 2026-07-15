# Changelog

## 0.3.7

- Terminal liveness: a consent answered from Telegram/panel no longer
  freezes the terminal — the `approve? y/n` prompt dismisses itself
  ("answered from another surface") and the turn keeps rendering live.
- Idle pickup: an open, idle terminal now picks up instructions sent from
  Telegram/panel within seconds and runs them as normal turns, tagged with
  their origin. If no terminal is open, the instruction waits (up to an
  hour) and Telegram says so instead of failing.

## 0.3.6

- Boot replay shows only the conversation: flattened tool traffic
  (`[tool_use …]` / `[tool_result] …` blocks the agent keeps as its own
  working memory) no longer floods the boot screen — each replayed message
  shows its human-readable part, pure tool messages are skipped.

## 0.3.5

- Surface-aware conversation: turns steered from Telegram/the panel render
  live in the terminal tagged by origin (`[telegram]` / `[web-panel]`) —
  the terminal stays the session's sole executor, foreign turns are
  display-only and are never executed locally.
- Boot replay: opening the terminal replays the recent conversation
  (including cross-surface turns) with origin tags, then a "— live —"
  divider. Best-effort — a network failure never blocks boot.

## 0.3.4

- `/notify [tg|panel|both|off]` — mirror this coding session to Telegram
  and/or the panel, and let either surface steer it back (approve/deny
  prompts, follow-up turns). No argument shows the current routing. Talks to
  the gateway's per-session remote-control state; a network hiccup notes
  cleanly instead of crashing the dock.

## 0.3.3

- Mouse-garbage / dead-keyboard fix (Linux, occasionally macOS): the dock now
  uses button-event mouse tracking instead of any-event — bare mouse movement
  no longer floods the terminal with reports that desynced the input parser,
  typed `35;6;42M…` fragments into the input box and fired phantom Escape
  presses that silently stopped the running turn. Scroll, click and
  drag-to-copy work as before. Leftover report fragments are scrubbed from
  the input, and a phantom Escape during a flood cleans the input instead of
  killing the turn.
- Untrusted text (tool output, relayed notes/summaries) is stripped of raw
  escape/control bytes before rendering, so it can never flip terminal modes.

## 0.3.2

- Wrapped lines keep their left gutter: a long note / progress / thinking /
  echoed-message line used to continue flush against the screen edge; every
  visual line now aligns to the same 2-column transcript gutter.

## 0.3.1

- Auto-checkpointing no longer disables itself after a single transient
  shadow-git error — it only pauses after repeated failures, and
  `/checkpoints` shows when it's paused.

## 0.3.0

- **The agent plans in the open.** A live todo checklist (📋) rendered from the
  kernel's todo facts — you see what Webbee intends and how far it's got.
- **Precise editing.** `edit_file` requires a unique match (or an explicit
  `replace_all`), and a new atomic `multi_edit` applies coordinated changes
  across files all-or-nothing.
- **The time machine.** Every change auto-checkpoints into a shadow git — your
  own `.git` is never touched; `/checkpoints` lists them and `/rollback`
  restores, and the agent can checkpoint/diff/rollback too. A wrong step is
  now undoable.

## 0.2.3

- **Readable big numbers.** Token and credit counts in the toolbar now scale
  (`1.5M tok`, `2M credits`) instead of an awkward `1500.0k`.
- **Esc really stops a turn now.** Previously only Ctrl-C worked; Esc now cancels
  the running turn too, matching the "Esc/Ctrl-C to stop" hint.
- **The `❯` prompt takes your mode's colour** — cyan (default), purple (plan),
  yellow (autopilot) — so the current mode is obvious from the input line.

## 0.2.2

- **See Webbee's reasoning as a distinct 💭 block.** Before each step, Webbee now
  shows a genuine line of thinking — what it's weighing and why — rendered as its
  own 💭 block, separate from status lines. Works on every model.
- **SSH steps are visible.** When Webbee runs a command on one of your connected
  servers, the terminal now shows that step (command + result), like any other.

## 0.2.1

- **Fixes a marathon that could hang on "working" and quietly burn credits.** A
  marathon works through milestones and streams a result at each one; the
  terminal used to stop at the first milestone, abandoning the rest of the run —
  the agent kept going server-side with nobody to run its tools, spinning until
  your balance drained. The terminal now follows a marathon to the end: it keeps
  working across milestones and stops only when the whole goal is done, it
  pauses (low credits / awaiting approval), or you stop it. Requires the current
  cloud; older milestones are unaffected.

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
