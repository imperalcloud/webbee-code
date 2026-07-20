import asyncio
import re
import time

from rich.align import Align
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from webbee.banner_art import WEBBEE_CODE
from webbee.sizing import trunc

_GUTTER = 2   # left margin (cols) — the single consistent transcript gutter.
              # Chrome lines prefix a 2-space string ("  "); block renderables
              # (Markdown answer, Panels, Table) are wrapped in _pad() to match,
              # so NOTHING renders flush against the screen edge.


def _pad(renderable):
    """Indent a block renderable by the transcript gutter so its left edge
    lines up with the 2-space chrome ('  🐝 Webbee', '  13.9s · …', the ❯ bar)."""
    return Padding(renderable, (0, 0, 0, _GUTTER))


# Untrusted content (tool output, kernel-relayed text) must never carry raw
# escape/control bytes into the terminal: a \x1b[?1003h inside a printed tool
# summary silently flips the terminal into any-event mouse tracking (the
# mouse-garbage bug's evil twin); OSC can retitle/exfiltrate. Rich's Text
# passes raw \x1b through untouched, so we strip at the sink: CSI and OSC
# sequences whole, then every remaining C0 control byte except \n and \t.
_CTRL = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"            # CSI (incl. private modes)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"   # OSC (BEL/ST or unterminated)
    r"|[\x00-\x08\x0b-\x1f\x7f]"             # other C0 + DEL (keeps \t \n)
)


def _clean(s) -> str:
    """Strip escape sequences + control bytes from untrusted display text."""
    return _CTRL.sub("", str(s or ""))

_ICON = {"read_file": "📖", "grep": "🔎", "glob": "🗂️", "write_file": "✎",
         "edit_file": "🔧", "bash": "⚡"}
_BEE = "yellow"       # bee-yellow brand accent — logo / 🐝 / notes / busy dot ONLY
_ACCENT = "cyan"      # interactive chrome ONLY — live caret / mode / panel url

# ---- welcome copy (single source of truth for the splash + its tests) ------
# The privacy promise is claims-disciplined: every clause is TRUE and enforced
# today — user data is never sold and never used to train any model (ours or
# third-party; documented in the DPA), and PII is masked before tool output
# re-enters the model (platform-enforced and covered by the platform's own
# test suite). Deliberately NOT said: "we collect nothing" (sessions ARE
# stored so Webbee can resume your work) and "guaranteed" (we say what is
# enforced, not what is promised). Change the wording only with a source.
WELCOME_PRIVACY = "🔒 Your work stays yours — never sold, never training data."
WELCOME_PRIVACY_DETAIL = "PII is masked before it reaches the model."
WELCOME_HINT = "Type a task — Webbee runs it to completion · /help · Ctrl-D to exit"


def _fmt_tokens(n: int) -> str:
    """Compact count for the live toolbar: 900 -> '900', 2_100 -> '2.1k',
    1_500_000 -> '1.5M', 2_000_000 -> '2M', 3_200_000_000 -> '3.2B'. Used for
    both token counts and (integer) credits so big numbers stay readable."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    a = abs(n)
    if a < 1000:
        return str(n)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1000, "k")):
        if a >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(n)


def _salient_arg(args: dict) -> str:
    """The one human-readable arg to show for a tool/consent call (PURE) — a
    title/path/command/name over an opaque id; falls back to the first id-ish
    value. Keeps the feed readable instead of dumping a raw args dict."""
    if not isinstance(args, dict):
        return ""
    for k in ("title", "name", "path", "pattern", "command", "query", "q"):
        v = args.get(k)
        if v:
            return str(v)
    for k in ("id", "note_id", "folder_id", "task_id"):
        v = args.get(k)
        if v:
            return str(v)
    return ""


def _invalidate() -> None:
    """Redraw the running prompt_toolkit app if any. No-op when none is active
    (tests / non-tty fallback) — the fallback when no output pane is wired."""
    try:
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
        if app is not None:
            app.invalidate()
    except Exception:
        pass


class RichSink:
    """Rich implementation of TurnSink. Turn output (action feed, 🐝 answer,
    footer) is rendered by the Rich Console. In the full-screen dock the Console
    writes ANSI into the OutputPane's buffer (repl passes console=pane.console +
    on_output=pane.notify) and the pane shows it in a scrollable region above
    the fixed input; after each render this sink calls on_output() so the pane
    follows the tail. The live status (busy/elapsed/tokens) is the dock toolbar,
    fed by status(). Consent runs through the dock via an asyncio.Future
    (resolve_consent), with a sync input fallback for non-tty.

    Tests pass live_enabled=False + inject input_fn/clock and no on_output; then
    _nudge() is a harmless no-op and consent uses the injected input_fn."""

    def __init__(self, console=None, *, live_enabled=True, input_fn=input,
                 clock=time.monotonic, on_output=None):
        self.console = console or Console()
        self._live_enabled = live_enabled
        self._input = input_fn
        self._clock = clock
        self._on_output = on_output      # pane.notify (scroll+redraw) in the dock
        self.tokens = 0
        self.credits = 0
        self.session_tokens = 0
        self.session_credits = 0
        self._tools = 0
        self._started = None
        self._busy = False
        self._current = ""
        self._pending = ("", "")
        self._consent = None            # asyncio.Future while awaiting a reply
        self._consent_summary = ""
        # Cross-surface items already queued in the RUNNING kernel session
        # (full-queue-layer K1: task_queued/task_dequeued frames). The dock's
        # queue panel reads THIS list object every redraw — mutate in place
        # only (append/del/clear), never rebind.
        self.remote_pending: list = []
        # The repl's OWN type-ahead deque (the SAME object tui mutates),
        # shared by reference at boot. remote_queued reconciles against it:
        # a kernel task_queued echo carrying a local QueuedLine's steer_iid
        # is positive proof the inject landed — the local twin is removed so
        # one message is one row with ONE owner (the kernel). None until the
        # repl wires it (minimal sinks / unit tests without a queue).
        self.local_pending = None
        # The current coding checklist (sticky todo panel, 0.3.15) — the twin
        # of remote_pending: todos() replaces its contents in place on every
        # todo frame; the dock's todo panel reads THIS list object every
        # redraw. Sticky: it persists across turn ends (the panel stays up)
        # and clears only on /clear.
        self.current_todos: list = []
        self._todo_counts = (0, 0)      # kernel-reported (done, total)
        self._todos_dirty = False       # a turn touched the list -> end_turn records it
        self._reconnecting = 0          # stream transport down (W1 front-5): armed attempt#, 0 = none
        self._reconnect_since = None    # clock() at the moment the outage started
        self._turn_failed = False       # ERROR (not a user stop) ended the last turn (W1 front-3b)
        self._parked = False            # marathon PARKED this turn (W1 front-3b) -> end_turn keeps remote_pending

    def _nudge(self) -> None:
        """After output/state changes: let the pane follow the tail + redraw."""
        if self._on_output is not None:
            self._on_output()
        else:
            _invalidate()

    # ---- welcome ------------------------------------------------------------
    def welcome(self, account, cwd: str, surface: str) -> None:
        """One-time launch splash, trimmed to what a human actually needs:
        the WEBBEE CODE logo (the brand), ONE identity line (who am I / plan —
        plan status shows only when it needs attention), the privacy promise
        (true, enforced claims only — see WELCOME_PRIVACY above), and one hint
        line to get going. Runs BEFORE the dock starts. Clears the screen ONLY
        in the non-pane path (the full-screen dock owns its own alternate
        screen — clearing there would corrupt it).

        Every line is wrapped in `Align.center` (W2 front-2, replay-safe)
        instead of a baked `.center(w)` string: the OLD approach froze the
        BOOT width's padding into literal leading spaces, so a resize replay
        (OutputPane.reflow re-prints every retained record at the NEW width)
        left the splash off-center — Align re-centers on every replay because
        it measures the renderable and pads fresh against the console's
        width at PRINT time, not at construction time."""
        if self._on_output is None:
            self.console.clear()

        self.console.print()
        self.console.print(Align.center(Text(WEBBEE_CODE, style=f"bold {_BEE}")))
        self.console.print(Align.center(Text("🐝", style=f"bold {_BEE}")))
        self.console.print(Align.center(Text("ICNLI AI Cloud OS · Agent", style=f"bold {_ACCENT}")))
        self.console.print(Align.center(Text("·  i m p e r a l . i o  ·", style="dim")))
        self.console.print()
        if account.signed_in:
            who = account.email or ""
            if account.nickname:
                who += f" · @{account.nickname}"
            if account.plan:
                who += f" · {account.plan} plan"
                if account.plan_status and account.plan_status != "active":
                    who += f" ({account.plan_status})"
            self.console.print(Align.center(
                Text.assemble(("Signed in as ", "dim"), (who, "white"))))
        else:
            self.console.print(Align.center(Text("not signed in — /login", style="dim")))
        self.console.print()
        self.console.print(Align.center(Text(WELCOME_PRIVACY, style="white")))
        self.console.print(Align.center(Text(WELCOME_PRIVACY_DETAIL, style="dim")))
        self.console.print()
        self.console.print(Align.center(Text(WELCOME_HINT, style="dim")))
        self.console.print()
        self._nudge()

    # ---- turn lifecycle -------------------------------------------------
    def begin_turn(self) -> None:
        self._tools = 0
        self._started = self._clock()
        self._current = ""
        self._pending = ("", "")
        self.tokens = 0        # per-turn live counters (usage frames are per-turn cumulative)
        self.credits = 0
        self._busy = True
        self._reconnecting = 0
        self._reconnect_since = None
        self._turn_failed = False
        self._parked = False
        self.console.print()   # breathing room between the user's message and the response
        self._nudge()

    def mark_turn_failed(self) -> None:
        """The turn ended in an ERROR (not a user stop): the dock's drain rule
        holds the type-ahead queue instead of burning one queued line into
        each failing turn (W1 front-3b). One-turn flag; begin_turn clears."""
        self._turn_failed = True

    def marathon_parked(self, reason: str) -> None:
        """The run PARKED (runaway/consent/credits — kernel keeps its task
        queue alive waiting for a wake). end_turn must NOT clear the remote
        rows: they are still queued server-side; the panel would lie
        (W1 front-3b, the post-runaway 'empty panel over a non-empty kernel
        queue'). Rows re-tag ⏸ parked; the next run's task_dequeued frames
        remove them by iid as the kernel drains."""
        self._parked = True

    def end_turn(self, final_text: str) -> None:
        self._busy = False
        # The kernel session's own queue only exists while a run is live —
        # once this turn returns COMPLETE or user-stopped, the terminal no
        # longer streams its dequeue frames, so any leftover remote rows
        # would linger as phantoms: clear them (in place — the panel holds
        # this list object). A PARKED turn is different: the kernel queue is
        # still alive server-side waiting for a wake, so the rows are kept
        # (tagged parked, ⏸ in the panel) instead of wiped — the next run's
        # task_dequeued frames remove them by iid as the kernel drains.
        if self._parked:
            for r in self.remote_pending:
                r["parked"] = True
            self._parked = False
        else:
            self.remote_pending.clear()
        # Sticky-todo scrollback record (0.3.15): in the dock the live panel
        # replaced the inline re-renders, so print the FINAL checklist state
        # ONCE per turn that touched it — the transcript keeps the history
        # while the panel itself persists (sticky) into idle.
        if self._on_output is not None and self._todos_dirty and self.current_todos:
            done, total = self._todo_counts
            self._todos_inline(self.current_todos, total, done)
        self._todos_dirty = False
        final_text = _clean(final_text)
        if final_text:
            self.console.print()   # separation before the focus block
            self.console.print(Text("  🐝 Webbee", style=f"bold {_BEE}"))
            self.console.print(_pad(Markdown(final_text)))   # body aligns to the same 2-col gutter as the header
        elapsed = self._elapsed()
        self.session_tokens += self.tokens
        self.session_credits += self.credits
        noun = "action" if self._tools == 1 else "actions"
        self.console.print(Text(
            f"  {elapsed:.1f}s · {self._tools} {noun} · {_fmt_tokens(self.tokens)} tok"
            f" · session {_fmt_tokens(self.session_tokens)} tok",
            style="dim"))
        self.console.print()   # breathing room before the next prompt
        self._nudge()

    def note(self, message: str) -> None:
        # _pad (not a "  " prefix) so a wrapped line keeps the gutter — a bare
        # prefix indents only the first visual line and continuations hug the
        # screen edge.
        self.console.print(_pad(Text(_clean(message), style=_BEE)))
        self._nudge()

    def reconnecting(self, attempt: int, delay: float) -> None:
        """Stream transport down (W1 front-5): attempt>0 arms the toolbar's
        `⟳ reconnecting` state (busy stays true — the steer poller keeps off);
        attempt==0 clears it, printing ONE honest note when the outage lasted
        >300s (the 24h/10k durable stream may have trimmed frames past our
        Last-Event-ID — progress lines may be missing, work is NOT lost)."""
        if attempt:
            if self._reconnect_since is None:
                self._reconnect_since = self._clock()
            self._reconnecting = int(attempt)
        else:
            since, self._reconnect_since = self._reconnect_since, None
            self._reconnecting = 0
            if since is not None and self._clock() - since > 300:
                self.note("⟳ reconnected after a long outage — some progress lines may be missing")
        self._nudge()

    def todos(self, items: list, total: int, done: int) -> None:
        """Checklist state for the coding TODO scratchpad. The kernel
        republishes the FULL list on every todo_write; each update replaces
        `current_todos` IN PLACE (the dock's sticky todo panel holds this
        list object and re-reads it every redraw — 0.3.15, no more inline
        scroll-away in the dock; end_turn prints the final checklist ONCE as
        the scrollback record). Headless / non-dock sinks (no on_output pane)
        keep today's full inline render. Defensive both ways: malformed items
        are skipped, bad counts degrade, never raises."""
        rows = []
        for item in (items if isinstance(items, (list, tuple)) else ()):
            if not isinstance(item, dict):
                continue                       # malformed entry — skip, never raise
            content = _clean(item.get("content", "")).strip()
            if not content:
                continue
            rows.append({"content": content,
                         "status": str(item.get("status", "") or "")})
        self.current_todos[:] = rows           # in place — the panel holds this list
        try:
            self._todo_counts = (int(done), int(total))
        except (TypeError, ValueError):
            self._todo_counts = (sum(1 for r in rows if r["status"] == "completed"),
                                 len(rows))
        self._todos_dirty = True
        if self._on_output is not None:        # dock: the sticky panel renders it live
            self._nudge()
            return
        self._todos_inline(items, total, done)

    def _todos_inline(self, items, total, done) -> None:
        """The Claude-Code-style INLINE checklist render (the pre-0.3.15
        behavior, verbatim): one line per item — ✓ completed (dim, struck) ·
        ▶ in progress (bold, so "what's happening now" pops) · ○ pending
        (muted), order preserved. Used by the headless/non-dock path on every
        todo frame and by end_turn's one-shot scrollback record in the dock."""
        try:
            head = f"📋 Todos ({int(done)}/{int(total)})"
        except (TypeError, ValueError):
            head = "📋 Todos"
        body = Text()                          # empty base style — segments below
        body.append(head, style=f"bold {_BEE}")   # carry their OWN, no bleed-through
        for item in (items if isinstance(items, (list, tuple)) else ()):
            if not isinstance(item, dict):
                continue                       # malformed entry — skip, never raise
            content = _clean(item.get("content", "")).strip()
            if not content:
                continue
            status = str(item.get("status", "") or "")
            if status == "completed":
                glyph, g_style, t_style = "✓", "green", "dim strike"
            elif status == "in_progress":
                glyph, g_style, t_style = "▶", f"bold {_BEE}", "bold"
            else:                              # pending / unknown -> not started yet
                glyph, g_style, t_style = "○", "grey66", "grey66"
            body.append(f"\n  {glyph} ", style=g_style)
            body.append(content, style=t_style)
        self.console.print(_pad(body))
        self._nudge()

    def user_echo(self, text: str) -> None:
        """Commit the just-sent user message as its own clearly-readable line
        with a background bar (NOT boxed like the live input) so it stands out
        as 'what I sent' in the scrollback."""
        self.console.print(_pad(Text(" ❯ " + _clean(text) + " ", style="bold white on grey30")))
        self._nudge()

    def remote_queued(self, origin: str, text: str, iid: str) -> None:
        """Full-queue-layer K1 (`task_queued` frame): a follow-up queued into
        the RUNNING kernel session from another surface shows in the live
        queue panel the instant it queues — tagged `[origin]`, "as if typed".
        DISPLAY-ONLY: it renders above the local rows but is never pullable
        (↑/click) — the kernel owns it; only a `task_dequeued` (or turn end)
        removes it. This is the ONE reconciliation chokepoint (0.3.16): the
        steer_iid minted at enqueue time dedups the display too — a frame
        delivered twice (publish retry / SSE resume) never doubles a row, and
        the echo of a terminal-injected line whose POST merely LOOKED failed
        removes its local fallback twin (the echo is positive proof it
        landed; kernel-owned = one row). Empty iid (legacy kernels) always
        appends — text is never a dedup key."""
        iid = str(iid or "")
        if iid:
            if any(r.get("iid") == iid for r in self.remote_pending):
                self._nudge()
                return
            lp = self.local_pending
            if lp is not None:
                twin = next((q for q in lp if getattr(q, "iid", "") == iid), None)
                if twin is not None:
                    try:
                        lp.remove(twin)
                    except ValueError:
                        pass
        self.remote_pending.append({"origin": _clean(str(origin or "")),
                                    "text": _clean(str(text or "")),
                                    "iid": iid})
        self._nudge()

    def remote_dequeued(self, origin: str, iid: str) -> None:
        """The kernel drained (or dedup-dropped) one queued item
        (`task_dequeued` frame): remove its panel row by `iid`. The
        oldest-same-origin fallback is LEGACY-ONLY (empty iid — the kernel
        queue is FIFO, so the oldest row of that origin is the one that just
        started): a NON-empty unmatched iid is a no-op, because the kernel
        emits one dequeue per pop INCLUDING dedup-dropped twins, and a twin's
        second dequeue must never eat a different same-origin row. Nothing
        matched → no-op (the row was already cleared or never announced)."""
        iid = str(iid or "")
        origin = _clean(str(origin or ""))
        rows = self.remote_pending
        idx = next((i for i, r in enumerate(rows) if iid and r.get("iid") == iid),
                   None)
        if idx is None and not iid:
            idx = next((i for i, r in enumerate(rows) if r.get("origin") == origin),
                       None)
        if idx is None:
            return
        del rows[idx]
        self._nudge()

    def queued_run(self, remaining: int) -> None:
        """The tiny lifecycle marker printed right before a drained queued
        line starts: `▶ running queued message` (+ how many still wait) — a
        drain is never a silent start; the drained text's normal ❯ user_echo
        follows immediately."""
        tail = f" · {remaining} still queued" if remaining else ""
        self.console.print(_pad(Text.assemble(
            ("▶ ", f"bold {_BEE}"),
            (f"running queued message{tail}", "italic grey66"))))
        self._nudge()

    def foreign_turn(self, surface: str, role: str, text: str) -> None:
        """One tagged line for a turn that lives on ANOTHER surface (a
        Telegram/panel-steered turn on the shared stream, or a replayed prior
        turn). DISPLAY-ONLY: never touches _tools/tokens/_busy, so the
        terminal's own turn accounting stays uncontaminated."""
        who = "you" if role == "user" else "🐝 webbee"
        surface = _clean(surface)
        tag = "" if surface in ("", "terminal") else f" [{surface}]"
        self.console.print(_pad(Text(f"{who}{tag}: {_clean(text)}", style=_BEE)))
        self._nudge()

    def consent_dismissed(self, note: str) -> None:
        """Liveness A: the pending consent was answered on ANOTHER surface
        (e.g. relayed from Telegram) — the kernel park is over. Retire the
        pinned prompt so the dock leaves `approve? y/n`: cancel the armed
        Future (consent_pending() flips False, so the toolbar repaints out of
        the consent state on the next _nudge) and print ONE note-style line
        so the scrollback shows why the y/n prompt vanished."""
        if self._consent is not None and not self._consent.done():
            self._consent.cancel()
        self._consent = None
        self._consent_summary = ""
        self.console.print(_pad(Text(_clean(note), style=_BEE)))
        self._nudge()

    def clear(self) -> None:
        """/clear: wipe the pane/screen + reset the session counters. The
        sticky todo panel resets with it (in place — the panel holds the
        list object); a mid-run abort deliberately does NOT (the last known
        plan state is still true and the panel is always-on)."""
        self.console.clear()
        self.tokens = 0
        self.credits = 0
        self.session_tokens = 0
        self.session_credits = 0
        self._tools = 0
        self._current = ""
        self.current_todos.clear()
        self._todos_dirty = False
        self._nudge()

    def abort(self) -> None:
        """Ctrl-C mid-turn: clear busy so the toolbar drops back to idle. No
        printing — the caller (repl.py) prints the note. Remote rows clear
        too — the stream is gone, their dequeue frames can never arrive."""
        self._busy = False
        self.remote_pending.clear()
        self._nudge()

    # ---- TurnSink -------------------------------------------------------
    def tool_start(self, tool: str, args: dict) -> None:
        self._tools += 1
        arg = args.get("path") or args.get("pattern") or args.get("command") or ""
        self._pending = (tool, str(arg))
        w = self.console.width
        self._current = f"{tool} {str(arg)[:trunc(w, 0.3, 40)]}".strip()
        self._nudge()  # the completed line is printed in tool_result

    def tool_result(self, tool: str, ok: bool, summary: str) -> None:
        # One calm dim line: icon + tool (+arg), then the ✓/✗ RIGHT NEXT TO the
        # action (not pinned to the far right), then a dim summary. Only the
        # ✓/✗ carries colour — the rest recedes (dim). Truncation lengths are
        # a PROPORTION of the live console width (W2 front-2), floored at the
        # old hardcoded counts so a narrow terminal never sees an empty line.
        _tool, arg = self._pending if self._pending[0] else (tool, "")
        icon = _ICON.get(_tool, "⚡")
        mark = "✓" if ok else "✗"
        w = self.console.width
        self.console.print(Text.assemble(
            ("  " + icon + " ", "dim"),
            (_clean(_tool), "dim"),
            (("  " + _clean(arg)[:trunc(w, 0.3, 40)]) if arg else "", "dim"),
            ("   ", ""),
            (mark + " ", "green" if ok else "red"),
            (_clean(summary)[:trunc(w, 0.35, 50)], "dim"),
        ))
        self._pending = ("", "")
        self._nudge()

    async def ask_consent(self, app_id: str, tool: str, args: dict) -> str:
        """Ask for consent and return the user's RAW reply (trimmed only) —
        NEVER interpret (the kernel decides, ICNLI). When the dock is running
        the reply comes through the pinned box via an asyncio.Future (no
        blocking input on the event loop); otherwise fall back to the injected
        sync reader (tests / non-tty)."""
        label = _clean(f"{app_id}·{tool}" if app_id else tool)
        sal = _clean(_salient_arg(args))
        w = self.console.width
        self.console.print(Text.assemble(("  ? approve ", "yellow"), (label, "dim"),
                                          (("  " + sal[:trunc(w, 0.4, 60)]) if sal else "", "dim")))
        fut = self._arm_consent(label, sal)
        if fut is None:                       # non-tty / no running app
            raw = self._input("     ")
        else:
            self._nudge()
            raw = await fut
        self._consent = None
        self._consent_summary = ""
        raw = (raw or "").strip()
        self.console.print(Text("  ↳ " + raw, style="dim"))   # quiet echo of the reply
        self._nudge()
        return raw

    async def ask_yes_no(self, question: str, timeout: float = 60.0) -> bool:
        """Terminal-LOCAL one-tap confirm for a remotely-requested privilege
        upgrade (the autopilot safe-asymmetry): print the question and arm
        the SAME pinned-input future the consent prompt uses (the dock's
        Enter handler routes the raw reply here; the toolbar flips to the
        reply state). STRICT gate — True only on an explicit local yes
        (y/yes); n, an empty reply, no dock, a prompt error or the timeout
        all return False, so the caller keeps the current mode. This is a
        LOCAL policy decision, not a kernel consent, so the reply is
        interpreted here rather than relayed (ICNLI raw-relay applies to
        kernel consents only)."""
        self.console.print(_pad(Text("⚠ " + _clean(question), style=f"bold {_BEE}")))
        fut = self._arm_consent(question, question)
        try:
            if fut is None:                    # non-tty / no dock → sync reader
                raw = self._input("     allow? [y/n] ")
            else:
                self._nudge()
                raw = await asyncio.wait_for(fut, timeout)
        except Exception:                      # timeout / prompt error → decline
            raw = ""
        finally:
            self._consent = None
            self._consent_summary = ""
        raw = (raw or "").strip().lower()
        self.console.print(Text("  ↳ " + (raw or "(no reply)"), style="dim"))
        self._nudge()
        return raw in ("y", "yes")

    def panel_release(self, panel_url: str, summary: str) -> None:
        body = Text.assemble(
            (summary + "\n\n" if summary else "", "white"),
            ("Approve it in your browser:\n", "white"),
            (f"  {panel_url}\n", f"bold {_ACCENT}"),
            ("Then ask again — you weren't charged.", "dim"),
        )
        self.console.print(_pad(Panel(body, title="💳 This costs money", border_style="magenta")))
        self._nudge()

    def login_prompt(self, user_code: str, url: str) -> None:
        """Device-code sign-in: show the URL to open + the code to enter, as a
        clear framed block. A bare print would be invisible in the full-screen
        dock, so this renders into the feed; the terminal then polls silently
        until you authorize in the browser."""
        body = Text.assemble(
            ("Open this URL in any browser (a phone is fine):\n", "white"),
            (f"  {url}\n\n", f"bold {_ACCENT}"),
            ("and enter this code:\n", "white"),
            (f"  {user_code}\n\n", f"bold {_BEE}"),
            ("Waiting for you to authorize…", "dim"),
        )
        self.console.print(_pad(Panel(body, title="🐝 Connect this terminal", border_style=_BEE)))
        self._nudge()

    def sessions_table(self, sessions) -> None:
        """Active sessions as a compact table (English-only): #, session, IP,
        last-seen, and a bee-yellow 'this device' marker for the current one."""
        from rich.table import Table
        self.console.print(Text("  Active sessions", style=f"bold {_BEE}"))
        if not sessions:
            self.console.print(Text("  (none)", style="dim"))
            self._nudge()
            return
        t = Table(show_header=True, header_style="dim", box=None, padding=(0, 3, 0, 0))
        t.add_column("#", style="dim", justify="right")
        t.add_column("session")
        t.add_column("ip", style="dim")
        t.add_column("last seen", style="dim")
        t.add_column("")
        for i, s in enumerate(sessions, 1):
            label = str(s.get("label") or s.get("surface") or "?")
            ip = str(s.get("ip_address") or "-")
            seen = str(s.get("last_seen_at") or "")[:16].replace("T", " ")
            here = Text("this device", style=f"bold {_BEE}") if s.get("current") else Text("")
            t.add_row(str(i), label, ip, seen, here)
        self.console.print(_pad(t))
        self.console.print(Text("  /sessions revoke <#>  ·  /logout-others", style="dim"))
        self._nudge()

    def step_detail(self, detail: dict) -> None:
        """P1b drill-down: one bordered block — facts row + bounded
        args/result previews (already PII-masked server-side)."""
        def _prev(p):
            p = p or {}
            note = f"  (truncated, {p.get('total_bytes', 0)} bytes total)" if p.get("truncated") else ""
            return (_clean(p.get("preview", ""))[:2000]) + note

        mark = "✓" if detail.get("ok") else "✗"
        head = (f"{mark} {detail.get('app_id', '')}·{detail.get('tool', '')}  "
                f"{(detail.get('duration_ms', 0) or 0) / 1000:.1f}s  "
                f"trace {detail.get('trace_id', '')}")
        body = f"{head}\n\nargs:\n{_prev(detail.get('args'))}\n\nresult:\n{_prev(detail.get('result'))}"
        self.console.print(_pad(Panel(body, border_style="dim", title="step detail", title_align="left")))
        self._nudge()

    def progress(self, text: str) -> None:
        if text:
            self.console.print(_pad(Text(_clean(text), style="dim italic")))
            self._nudge()

    def thinking(self, text: str) -> None:
        # System-driven reasoning as a distinct 💭 block — visually apart from the
        # dim `progress` line (which stays reserved for status like low-balance).
        if text:
            self.console.print(_pad(Text("💭 " + _clean(text), style="italic medium_purple3")))
            self._nudge()

    def plan_blocked(self, tool: str) -> None:
        """Plan mode auto-declines writes/destructive. Tell the user WHY and how
        to allow it (Shift+Tab). Autopilot and default never reach this."""
        self.console.print(Text.assemble(
            ("  ⛔ plan mode", _BEE),
            (f" — {tool} blocked. " if tool else " — action blocked. ", "dim"),
            ("Press Shift+Tab to switch to default or autopilot to allow it.", "dim")))
        self._nudge()

    def usage(self, tokens: int, credits: int) -> None:
        # Cumulative frame — trust the server's running totals verbatim.
        # Slice C: CREDITS (not raw $) — the kernel keeps the $ server-side.
        self.tokens = tokens
        self.credits = credits
        self._nudge()

    # ---- dock bridge (read by tui.run_session's toolbar + Enter binding) ---
    def status(self) -> dict:
        """Live state for the dock toolbar. The bottom counter shows the SESSION
        TOTAL (not per-turn): the running session total, plus the in-flight
        turn's spend while busy (so it grows live), with no double-count at idle
        (end_turn has already folded the finished turn into the session)."""
        return {"busy": self._busy, "current": self._current,
                "elapsed": self._elapsed(), "tools": self._tools,
                "tokens": self.session_tokens + (self.tokens if self._busy else 0),
                "credits": self.session_credits + (self.credits if self._busy else 0),
                "consent": self.consent_pending(),
                "reconnecting": self._reconnecting,
                "turn_failed": self._turn_failed}

    def is_busy(self) -> bool:
        return self._busy

    def consent_pending(self) -> bool:
        return self._consent is not None and not self._consent.done()

    def resolve_consent(self, raw: str) -> None:
        """Called by the dock's Enter binding when a consent reply is awaited —
        hands the RAW reply verbatim to the awaiting ask_consent (ICNLI)."""
        if self.consent_pending():
            self._consent.set_result(raw)

    # ---- internals ------------------------------------------------------
    def _arm_consent(self, label: str, summary: str):
        """Create the consent Future iff a dock is running; else None (caller
        falls back to sync input)."""
        try:
            from prompt_toolkit.application import get_app_or_none
            if get_app_or_none() is None:
                return None
            self._consent = asyncio.get_running_loop().create_future()
            self._consent_summary = summary or label
            return self._consent
        except Exception:
            return None

    def _elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return self._clock() - self._started
