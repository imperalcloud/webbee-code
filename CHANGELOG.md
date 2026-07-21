# Changelog

## 0.3.26

The Home tab is now an interactive dashboard — a little website inside your
terminal. Everything is clickable and keyboard-navigable, with a highlight
that follows your focus and the mouse.

- Home shows your account and plan, your credits balance, your open tabs
  (with each tab's mode, a close button, and how much that session has
  spent), your recent repositories (one click reopens one in a new tab),
  the devices you're signed in on, and a small Settings panel.
- Settings you can change right from Home: the mode new tabs open in,
  where a running session sends notifications, and a Top-up credits button.
- A Trust & security panel explains, in plain terms, how your data is
  handled — with a link to the full security and privacy docs.
- The new-tab "+" button is now bee-yellow and easy to spot.
- Ctrl+T now opens a new tab, like a browser (it used to jump to Home).
  Home is still one click away on its own tab (or Alt+0).

## 0.3.25

Tabs name themselves after your first message, a + button opens a tab like
a browser, the tab bar got breathing room and a proper bar background, and
stray terminal codes can't pollute the input anymore.

- A session tab now renames itself from your first message — like a
  browser page title — so you can tell tabs apart at a glance instead of
  seeing the same folder name everywhere. `/rename <name>` sets your own
  title any time and it sticks (auto-naming never overrides it).
- A new + button at the end of the tab bar opens a new tab, just like a
  browser — no more remembering the keyboard shortcut.
- The tab bar now sits on its own subtle bar background with a blank row of
  breathing room below it, separating it clearly from your conversation.
- Clicking the ✕ or the + button now only reacts to a precise click on the
  glyph itself — a near-miss next to it no longer accidentally closes a tab.
- Closing a tab that's still busy now asks you to confirm first (click ✕
  again) — the server-side run keeps going either way, this just protects
  against an accidental close.
- Opening the same project in two separate windows no longer makes them
  freeze each other — the second window notices and starts its own
  parallel session automatically.
- Stray terminal focus-tracking codes (from switching windows or tmux
  panes) can no longer leak into what you're typing.

## 0.3.24

The tab bar got a real design: the active tab is a solid highlighted chip,
tabs are clearly separated, and spacing is uniform — plus a message typed
on Home no longer shows up twice in the new tab.

- Tabs now render as padded chips with uniform spacing, a clear dim
  separator between every pair, and the active tab as a solid
  bee-yellow block — unmistakable at a glance, no more squinting to find
  which tab you're on.
- Typing a task on Home and landing in the new session tab no longer
  double-prints your first message in that tab's transcript.
- Each tab now keeps its own input draft — switch away and back, your
  unsent text is right where you left it.

## 0.3.23

If a task on your session is waiting for your terminal — sent from the panel
or Telegram while the terminal was idle — the terminal now picks it up
within seconds instead of the task stalling.

- An idle terminal now notices when a running task on its own session is
  waiting for a tool approval or result, and reconnects to it automatically
  — no more silent stalls that eventually get marked "unresponsive".

## 0.3.22

Open the same project in two tabs and they truly run in parallel — each tab
gets its own isolated working copy automatically; remote messages land in the
right tab.

- Two tabs on the same repo no longer share one working copy: the second tab
  gets its own isolated checkout automatically, so edits from one tab never
  collide with the other. A note in that tab tells you it happened (or, on
  the rare case it couldn't, that the checkout is shared so you know to be
  careful).
- Telegram/panel messages and remote mode switches now reach the RIGHT tab —
  every open tab is independently steerable, not just whichever one a single
  shared listener happened to be watching.
- `/tabs` now shows each tab's short id for reference.

## 0.3.21

- A mode change sent from the panel or Telegram now reliably reaches your
  coding session — whichever tab is on screen. Previously the request could
  land on the Home tab and vanish silently; the terminal kept its old mode
  while the panel claimed the switch happened.

## 0.3.20

Webbee remembers your mode per project (autopilot always asks again),
reports it so the panel shows the truth, and tells you at start when a
session is already running or waiting for an approval.

- Each repo remembers its own coding mode across restarts — open Webbee
  again in the same project and it picks up right where you left it.
  Autopilot is the one exception: it always asks again, every time, at this
  terminal — never silently resumed from a previous run.
- Your terminal now reports its real coding mode back, so the panel and
  Telegram stop guessing and show the truth.
- On start, if a session is already running in this repo, Webbee tells you
  it reattached — and if it's waiting on an approval, that the prompt will
  re-show (or that you can approve it from the panel). If a DIFFERENT repo
  has a session parked on an approval, you get a one-line heads-up too.

## 0.3.19

The browser wave: Webbee Code becomes a browser inside your terminal.

- A tab bar lives at the top: ◆ Home first, your sessions after it, each with
  a live status glyph (▶ working · ⚠ waiting for your approval · ○ idle).
  Click a tab or press Alt+0..9 to switch; Ctrl+T jumps to Home; Ctrl+W or
  /close closes a tab (the run keeps living server-side); the ✕ on a tab
  closes exactly that tab.
- Home is a new-tab page: your identity and plan, every open tab, the current
  repo's intelligence and checkpoints, remote-control state and updates —
  filled live, resize-aware. Start typing on Home and a new session tab opens
  with your message. A fresh install lands on Home; an ongoing conversation
  lands back in its session tab.
- Every tab is its own world: transcript, queue, todos, approvals, mode and
  input history never leak between tabs. Background tabs keep streaming; a
  tab waiting for your approval shows a ⚠ badge from anywhere; a running
  background turn is protected from a stray Ctrl+D.

## 0.3.18

The adaptive wave: the terminal finally behaves like a real app.

- Stretch or shrink the window and the WHOLE transcript re-wraps live to the
  new size — history included, splash re-centered, nothing clipped, no dead
  margins. Panels, the input box and text truncations now scale as
  proportions of your screen, not fixed character counts.
- Select text with the mouse and drag past the edge — the transcript
  auto-scrolls under your selection, keeps selecting while you hold at the
  edge, and finishes the copy even if you release outside the pane. Scrolling
  mid-selection no longer corrupts what you copy, and a lost release can
  never hijack your next click or your clipboard.

## 0.3.17

Bulletproof core (W1): a marathon can no longer be killed by transport, the
queue survives every error class, and the client stops leaking memory.

- A gateway blip mid-run (502, deploy, network drop) no longer kills the turn:
  the stream patiently reconnects and resumes exactly where it left off, the
  toolbar shows an honest `⟳ reconnecting` state, and only a real sign-out
  ends the run — with a clear "run /login" message instead of a raw error.
- A stream 401 gets ONE forced token refresh before it counts as a sign-out,
  and a refresh that fails because the gateway was mid-deploy no longer burns
  that chance.
- Every message now carries a dedup key end-to-end, so a retried send after an
  ambiguous network failure can never execute the same instruction twice.
- The turn-start and result posts retry transient failures too — outage
  recovery drops from minutes to seconds.
- A turn that ends in an error HOLDS the queued messages (with an honest note)
  instead of burning them one failing turn at a time; a parked marathon keeps
  its queued rows visible, tagged ⏸.
- A stuck busy flag can no longer starve remote-message pickup; a message the
  kernel deduplicated ends its wait honestly instead of spinning forever.
- Pull a queued message to edit and resubmit it unchanged — it keeps its dedup
  identity.
- Click the queue or todo panel header to collapse it to one row (▸/▾).
- Performance: one keep-alive connection replaces a TLS handshake every 4s;
  the idle poll relaxes to 30s after 5 quiet minutes; hours-long marathons no
  longer grow memory without bound; transcript rendering is O(new output) per
  print; embedding vectors load memory-mapped.
- Windows groundwork: `.git` filters now match Windows paths (no reindex
  storms).

Companion release: imperal-mcp 0.5.2 — only a real 401 means "signed out";
gateway 5xx/network errors during a token refresh are retryable.

## 0.3.16

- A message typed while Webbee is working now shows in the queue panel
  exactly once: the queue reconciles every entry by its id, so a retried
  delivery or a send that only looked failed no longer produces a duplicate
  row.

## 0.3.15

- Type a follow-up while Webbee is working and it now flies into the current
  run within seconds (not after it finishes), shown in the queue tagged by
  where it came from. The task list is now a pinned panel that stays visible
  and updates live.

## 0.3.14

- See queued messages from Telegram/panel right in the terminal queue
  (tagged by origin), and set the coding mode from Telegram — switching to
  autopilot asks you to confirm in the terminal first.
- Reading a file now shows its size and freshness up front — line count,
  when it was last modified, what it defines, and what depends on it — so
  Webbee reads big files smartly and never edits a stale one; an edit to a
  file that changed on disk since it was read gets a re-read nudge.
- The code-intelligence graph now installs by default (`webbee[intel]`) —
  symbol awareness + repo relationships are on out of the box, not opt-in.

## 0.3.13

- The message queue is now a live panel above the input — you see exactly
  what's waiting, press ↑ to pull the last one back for editing (it leaves
  the queue and returns when you resend), or click one to edit it. The
  transcript stays clean.
- Cleaner welcome screen — just the essentials (who you are, your plan, how
  to start) plus a clear word on privacy: your work is never sold and never
  used to train models, and PII is masked before it reaches the model.

## 0.3.12

- Queued messages are now visible — when you type while Webbee is working,
  your message shows as `⋯ queued: …` right away, the toolbar shows the count,
  and it runs (clearly) after the current turn. New `/queue` and `/queue clear`
  to see and manage what's waiting — they work even mid-turn.
- Stopping a turn (Esc/Ctrl-C) no longer auto-runs the queue: it stays put,
  visible, until you clear it or a next turn finishes naturally.
- Task lists now render in full — every to-do with its status (✓ done ·
  ▶ doing · ○ next), redrawn on each update, so you always see the whole plan
  and where Webbee is in it.

## 0.3.11

- Type-ahead queue: send follow-ups while Webbee is working — they queue and
  run after the current turn; the toolbar shows how many are waiting.
- Up-arrow recalls your last message to edit and resend.

## 0.3.10

- Remote instructions picked up by an idle terminal now carry a dedup id, so
  an at-least-once delivery can never run — or bill — the same instruction
  twice.

## 0.3.9

- Internal restructure: the session engine was split into focused modules
  (consent handling, coding context, boot) — no behavior change.


## 0.3.8

- No more sign-in races: token refresh is serialized and retries once after
  a sibling terminal rotates the session — multiple open terminals no longer
  knock each other out with "session expired".
- No more dock lockup: a turn that fails (for example, an expired session)
  now clears the "working" state, and every key keeps responding even if the
  turn state ever goes stale. The idle pickup poller also backs off instead
  of hammering a signed-out session.

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
