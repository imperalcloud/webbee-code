"""Full-screen dock: a scrollable, colored output pane (Rich → ANSI, see
output_pane.py) fills the top; a bordered input box + toolbar are pinned at
the very bottom and never move while the output scrolls (mouse wheel /
PageUp). `run_session` also drives step-navigation (Up/Down + Enter) over the
pinned box when the input is empty and no turn is running; in every other
state Up/Down recall submitted lines (readline-style), and Enter while a turn
runs FLIES the line into the RUNNING turn (mid-turn inject, 0.3.15 — the
kernel absorbs it at the next brain step; its task_queued[terminal] echo
shows the panel row) with the local type-ahead queue as the fallback when no
inject leg is wired or it fails (shown LIVE in the queue panel pinned above
the input — see queue_panel.py — counted in the toolbar, run after the
current turn — natural completion only, a user STOP preserves the queue;
↑ on an empty input pulls the newest queued line back for editing, a click
pulls that item; /queue lists it, /queue clear drops it; the transcript
stays clean — real turns only). A STICKY todo panel (todo_panel.py) sits
above the queue panel and tracks the current checklist live. Pure helpers
(next_mode/build_toolbar/the *_action functions) are unit-tested; the
Application is TTY/headless-smoke verified. Grounded in prompt_toolkit
3.0.52."""
import asyncio
import re
from collections import deque

from webbee.output_pane import OutputPane  # noqa: F401 — re-exported (webbee.tui.OutputPane)
from webbee.queue_panel import pull_item, queue_fragments, queue_height
from webbee.render import _fmt_tokens
from webbee.todo_panel import todo_fragments, todo_height

_MODES = ("default", "plan", "autopilot")
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille frames — animated while a turn runs

# Leaked SGR mouse-report fragments ("<35;6;42M" / "35;6;42M"): under a
# mouse-move flood the vt100 parser splits sequences at read-chunk boundaries
# and the printable tail lands in the input buffer as literal text (live on
# Linux + occasionally macOS, 2026-07-12). Requires the full x;y;btn+M shape —
# ordinary "a;b;c" text never matches (a literal "35;6;42M" the user typed
# would be dropped too; accepted, it IS the residue shape).
_MOUSE_RESIDUE = re.compile(r"(?:\x1b\[)?<?\d{1,4};\d{1,4};\d{1,4}[Mm]")


def scrub_mouse_residue(text: str) -> str:
    """PURE. Drop leaked mouse-report fragments; everything else unchanged."""
    return _MOUSE_RESIDUE.sub("", text or "")


def configure_mouse_modes(output) -> None:
    """Replace prompt_toolkit's ANY-EVENT mouse tracking (?1003 — every bare
    mouse move fires a report) with BUTTON-EVENT tracking (?1002 — reports only
    while a button is held). Wheel scroll, clicks and drag-select all still
    work; the bare-move flood that desyncs the parser (phantom Escape + report
    tails typed into the input) disappears at the source. No-op for outputs
    without write_raw (non-vt100)."""
    if not hasattr(output, "write_raw"):
        return

    def _enable():
        output.write_raw("\x1b[?1000h")   # clicks + wheel
        output.write_raw("\x1b[?1002h")   # motion ONLY while a button is held
        output.write_raw("\x1b[?1015h")   # urxvt encoding
        output.write_raw("\x1b[?1006h")   # SGR encoding

    def _disable():
        output.write_raw("\x1b[?1002l")
        output.write_raw("\x1b[?1003l")   # belt & braces: clear any-event too
        output.write_raw("\x1b[?1000l")
        output.write_raw("\x1b[?1015l")
        output.write_raw("\x1b[?1006l")

    output.enable_mouse_support = _enable
    output.disable_mouse_support = _disable


def next_mode(mode: str) -> str:
    try:
        return _MODES[(_MODES.index(mode) + 1) % len(_MODES)]
    except ValueError:
        return _MODES[0]


def build_toolbar(mode: str, tokens: int, credits: int, *, busy: bool = False,
                  current: str = "", elapsed: float = 0.0, tools: int = 0,
                  consent: bool = False, queued: int = 0,
                  reconnecting: int = 0) -> list:
    """The status line under the pinned input box, as prompt_toolkit formatted
    text (per-segment styled). Four states: consent (awaiting a reply),
    reconnecting (the stream transport is down mid-turn — honest, not a fake
    spinner: the run continues server-side and resumes on reconnect), busy
    (a turn is running — an ANIMATED coloured spinner + the current action in
    accent, so it pops, not grey), and idle (mode value coloured PER MODE —
    default cyan / plan purple / autopilot yellow — + SESSION spend + the
    Shift + TAB hint). `queued` = type-ahead lines waiting to run after the
    current turn; when >0 the `⋯N queued` segment renders in the ACCENT class
    (tb.working, NOT dim) in the busy AND idle states, so the depth is
    noticeable at a glance. Style classes are defined in run_session's Style."""
    q = [("class:tb.working", f" · ⋯{queued} queued")] if queued else []
    if consent:
        return [("class:tb.consent", "  approve? type y / n / a reply · Enter to send")]
    if busy and reconnecting:
        frags = [("class:tb.consent", f"  ⟳ reconnecting ({reconnecting})"),
                 ("class:tb.dim", " · the run continues server-side")]
        frags += q
        frags.append(("class:tb.dim", "   ·   Esc/Ctrl-C to stop"))
        return frags
    if busy:
        spin = _SPINNER[int(elapsed * 10) % len(_SPINNER)]   # animates via the ticker
        frags = [("class:tb.spin", f"  {spin} "), ("class:tb.working", "working")]
        if current:
            frags += [("class:tb.dim", " · "), ("class:tb.action", current)]
        frags.append(("class:tb.dim",
                      f" · {elapsed:.0f}s · {tools} · {_fmt_tokens(tokens)} tok"))
        frags += q
        frags.append(("class:tb.dim", "   ·   Esc/Ctrl-C to stop"))
        return frags
    return [("class:tb.dim", "  mode: "),
            (f"class:tb.mode.{mode}", mode),
            ("class:tb.dim", f"   ·   {_fmt_tokens(tokens)} tok · {_fmt_tokens(credits)} credits"),
            *q,
            ("class:tb.dim", "   ·   Shift + TAB: switch mode")]


def _escape_action(sel: dict, turn: dict, is_busy, stop_turn, event, buf=None) -> None:
    """Esc key binding (P5g). While a turn is running, STOP it — cancel the LOCAL
    turn task (what actually tears the turn down, same as Ctrl-C) AND ask the
    server to stop (so it stops spending). While idle, clear the step-selection.

    Phantom-Esc guard (2026-07-12): a mouse-report flood splits sequences —
    the ESC arrives as a lone Escape KEY and the tail lands in the input
    buffer. Residue in the buffer ⇒ this Escape is almost certainly a split
    report, not the user: clean the buffer and KEEP the turn running."""
    if is_busy():
        if buf is not None and _MOUSE_RESIDUE.search(buf.text or ""):
            buf.text = scrub_mouse_residue(buf.text)
            return
        t = turn.get("task")
        if t is not None and not t.done():
            if stop_turn is not None:
                event.app.create_background_task(stop_turn())
            turn["stopped"] = True   # user STOP → the type-ahead queue must NOT auto-run
            t.cancel()                       # cancel the running turn; dock survives
        return
    sel["i"] = None
    event.app.invalidate()


def _interrupt_action(turn: dict, is_busy, stop_turn, event) -> None:
    """Ctrl-C key binding (P5g). The LOCAL task.cancel() is what actually
    tears the dock down (unchanged); ALSO ask the server to stop the turn so
    it doesn't keep running (and spending) after the dock moves on."""
    t = turn["task"]
    if t is not None and not t.done():
        if is_busy() and stop_turn is not None:
            event.app.create_background_task(stop_turn())
        turn["stopped"] = True   # user STOP → the type-ahead queue must NOT auto-run
        t.cancel()                          # cancel the running turn; dock survives


class QueuedLine(str):
    """A locally-queued type-ahead line that remembers the steer_iid minted at
    enqueue time (mid-turn inject fallback, 0.3.15). A plain str everywhere it
    already flows (panel one_line, pull_item→buffer, history, dispatch) — the
    iid rides along ONLY so the turn-end drain re-submits under the SAME dedup
    id: if a failed-looking inject actually landed server-side, the kernel's
    steer-iid ring drops the twin instead of running it twice."""
    iid = ""

    def __new__(cls, text: str, iid: str = ""):
        s = str.__new__(cls, text)
        s.iid = str(iid or "")
        return s


async def _inject_or_queue(inject, text: str, pending, invalidate=None) -> bool:
    """Enter-while-busy fly-in (mid-turn inject, 0.3.15): mint the steer_iid
    HERE — one id for both legs — and POST immediately via `inject(text, iid)`
    (the repl's gateway leg). REUSE, don't mint, when `text` already carries
    one (a QueuedLine — a pull-to-edit resubmitted UNCHANGED, see
    _rewrap_pulled): the original inject may have already landed server-side,
    so re-flying it under the SAME iid lets the kernel ring dedup the twin
    instead of running a genuine duplicate turn. On ok the line is
    KERNEL-owned: nothing is queued locally — the kernel's
    task_queued{origin:terminal} echo renders the panel row and
    task_dequeued clears it when the running turn absorbs it (seconds, the
    next brain step). On ANY failure (no live session yet, offline, gateway
    refusal) fall back to today's local queue: the row — carrying the SAME
    iid — drains at turn end through _drain_pending, stays ↑/click-pullable,
    and the kernel ring dedups the twin if the inject landed after all.
    Returns True when the line flew in (the caller's tests read it)."""
    from uuid import uuid4
    iid = getattr(text, "iid", "") or uuid4().hex
    try:
        ok = bool(await inject(text, iid))
    except Exception:
        ok = False
    if not ok:
        pending.append(QueuedLine(text, iid))
    if invalidate is not None:
        invalidate()
    return ok


def _submit_line(text: str, buf, pending, busy: bool, start, inject=None) -> str:
    """Route ONE submitted line (dependency-injected, same testing philosophy
    as _escape_action). Whitespace never queues nor starts ("ignored"). A real
    line is recorded into the buffer's history first (up-arrow recall), then:
    busy + an `inject` launcher wired → fly it into the RUNNING turn NOW
    (mid-turn inject, 0.3.15 — the launcher fires _inject_or_queue as a
    background task; a failed inject falls back to the local queue there);
    busy without a launcher → QUEUE it (Claude-Code type-ahead — the line is
    never erased or dropped; it runs after the current turn) — the LIVE queue
    panel above the input shows it at once (queue_panel.queue_fragments reads
    this deque every redraw), NEVER a static scrollback echo (those scrolled
    away, duplicated and went stale when edited); idle → `start(text)`
    (today's normal submit, unchanged).
    Returns "ignored" | "injected" | "queued" | "started"."""
    if not text.strip():
        return "ignored"
    buf.history.append_string(text)
    if busy:
        if inject is not None:
            inject(text)
            return "injected"
        pending.append(text)
        return "queued"
    start(text)
    return "started"


def _drain_pending(pending, start, mark=None) -> bool:
    """Turn-completion drain: pop the OLDEST queued type-ahead line and hand
    it to `start` — the SAME path a typed line takes, so a persistent
    marathon receives it as a `new_task` into the running session. ONE item
    per completion; the rest stay queued FIFO (each finished turn drains the
    next). `mark` (sink.queued_run) announces the handoff — `▶ running queued
    message` — right before the drained line's normal ❯ user-echo, so a drain
    is never a silent start. The popped line is ALREADY OUT of `pending` the
    moment it's read — a `mark` error must never lose it (swallowed: it's
    only a render-side announcement) and a `start` error must put it BACK at
    the head before propagating (a broken start must not silently vanish a
    queued line). Returns True when a drain happened."""
    if not pending:
        return False
    text = pending.popleft()
    if mark is not None:
        try:
            mark(len(pending))
        except Exception:
            pass          # a render error must never lose the popped line
    try:
        start(text)
    except Exception:
        pending.appendleft(text)
        raise
    return True


def _is_queue_command(text: str) -> bool:
    """PURE. `/queue` and its subcommands MANAGE the type-ahead queue, so they
    must run exactly when the queue matters — mid-turn. The Enter handler
    routes them past the busy gate (they never type-ahead-queue themselves)."""
    parts = (text or "").strip().lower().split()
    return bool(parts) and parts[0] == "/queue"


def _arrow_up_action(event, buf, sel: dict, n: int, busy: bool, pending=None,
                     pulled=None) -> None:
    """Up key — precedence: (1) QUEUE-PULL: pending items + an EMPTY buffer
    (busy or idle — NOT busy-gated: the queue legally survives a user STOP
    into idle, and pulling the newest to edit is exactly what you want after
    an Esc) pull the NEWEST queued line out of the queue into the input for
    editing; re-submit re-queues it at the tail (busy) or runs it (idle).
    Repeated presses walk newest→oldest, one item per press; a buffer with
    ANY text is never clobbered (history/step-nav serve it instead). When
    `pulled` (run_session's one-shot carry dict) is given, the pulled item's
    text + steer_iid are recorded into it so _rewrap_pulled can hand the SAME
    iid back if the line is resubmitted unedited (default None keeps the old
    behavior for direct-call tests that don't care).
    (2) Step-navigation EXACTLY as before (steps + empty input + idle —
    reachable exactly when the queue is empty, i.e. today's behavior verbatim
    in the queue-empty world). (3) Recall an older submitted line
    (readline-style)."""
    if pending and not buf.text:
        item = pull_item(pending, buf, len(pending) - 1)   # newest — "edit the last thing I queued"
        if item is not None and pulled is not None:
            pulled["text"], pulled["iid"] = str(item), getattr(item, "iid", "")
        event.app.invalidate()
        return
    if n and not buf.text and not busy:
        sel["i"] = (n - 1) if sel["i"] is None else max(0, sel["i"] - 1)
        event.app.invalidate()
        return
    buf.history_backward()
    event.app.invalidate()


def _arrow_down_action(event, buf, sel: dict, n: int, busy: bool) -> None:
    """Down key — mirror of _arrow_up_action: step-nav on the same exact
    gate, otherwise cycle history forward."""
    if n and not buf.text and not busy:
        sel["i"] = 0 if sel["i"] is None else min(n - 1, sel["i"] + 1)
        event.app.invalidate()
        return
    buf.history_forward()
    event.app.invalidate()


def _rewrap_pulled(pulled: dict, text: str):
    """PURE. A pulled queued line resubmitted UNCHANGED keeps its steer_iid so
    the kernel ring can still dedup a landed twin (W1 front-3b: pull-to-edit
    previously minted a fresh iid — a genuine duplicate turn when the original
    inject had landed). ANY edit = a genuinely new message = fresh iid (the
    landed twin said something else). One-shot: `pulled` is consumed (reset
    to empty) on every call, whether or not it held anything, so a later
    unrelated Enter never sees a stale carry."""
    iid, orig = pulled.get("iid", ""), pulled.get("text", "")
    pulled["iid"] = ""
    pulled["text"] = ""
    if iid and text == orig:
        return QueuedLine(text, iid)
    return text


async def run_session(*, pane, on_line, mode_getter, on_cycle, status,
                      is_busy, consent_pending, resolve_consent, steps_nav=None,
                      stop_turn=None, pending=None, queued_run=None,
                      remote_pending=None, todos=None, inject=None,
                      turn=None) -> bool:
    """The full-screen dock: `pane` fills the top (scrollable), a bordered input
    box + toolbar are FIXED at the bottom. Enter either resolves a pending
    consent reply (ICNLI: raw verbatim) or starts a turn as a BACKGROUND task
    (the box stays fixed during it); while a turn runs, Enter QUEUES the line
    (type-ahead — the LIVE queue panel pinned above the input shows it at
    once and tracks every edit/drain, the toolbar shows `⋯N queued` in
    accent, and it starts right after the current turn with a `queued_run`
    marker — the transcript itself stays real-turns-only). ↑ on an empty
    input pulls the newest queued line back into the box for editing (it
    leaves the panel; re-submit re-queues/runs it); clicking a panel row
    pulls THAT item. Either pull carries the item's steer_iid into the
    one-shot `pulled` dict; if the line is resubmitted BYTE-IDENTICAL it goes
    back out under the SAME iid (_rewrap_pulled) so the kernel ring can dedup
    a landed twin instead of running a genuine duplicate turn — ANY edit
    mints a fresh iid, since the landed twin said something else.
    `pending` is the queue deque — the repl passes its OWN
    so /queue and /queue clear (dispatched through the normal command layer)
    see and manage the live queue; /queue itself always runs immediately,
    even mid-turn. When `steps_nav` is given and the input is empty and no turn
    is running, Up/Down move a step selection (toolbar shows `step k/N`) and
    Enter expands it via `steps_nav["expand"]`; Esc clears it. In every other
    state Up/Down recall submitted lines (readline-style history).
    `remote_pending` (full-queue-layer K1) is the sink-owned list of items
    already queued in the RUNNING kernel session from other surfaces — the
    panel renders them ABOVE the local rows, tagged `[origin]`, DISPLAY-ONLY
    (never pullable via ↑/click; the kernel owns their drain). `inject`
    (mid-turn inject, 0.3.15) is the repl's async `inject(text, iid) -> bool`
    gateway leg: when wired, Enter-while-busy flies the line into the RUNNING
    turn immediately instead of holding it locally (fallback to the local
    queue on failure — see _inject_or_queue). `todos` is the sink-owned
    current-checklist list (RichSink.current_todos): a STICKY panel above the
    queue panel renders it live (todo_panel builders), zero rows when empty.
    `turn` (lockout-proof poller gate) lets the CALLER share its own turn
    dict (same object, keys `task`/`stopped`) instead of a private one --
    the repl passes its `turn_ref` so `_poller_busy` can read whether the
    turn task is genuinely alive, mirroring `_busy_live` below; omitted, a
    local dict is used (tests that don't care about the shared gate).
    Returns True on clean exit; False if prompt_toolkit is unavailable (caller
    uses the plain fallback loop)."""
    try:
        from prompt_toolkit.application import Application, get_app
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except Exception:
        return False

    buf = Buffer(multiline=False)
    if turn is None:
        turn = {"task": None}   # caller-shared (repl's poller gate reads it)
    turn.setdefault("task", None)
    if pending is None:
        pending = deque()   # type-ahead queue: lines submitted while a turn runs
    if remote_pending is None:
        remote_pending = []   # cross-surface kernel-queued items (display-only)
    if todos is None:
        todos = []            # sink-less callers: the todo panel never shows
    pulled = {"text": "", "iid": ""}   # one-shot carry: pull-to-edit's steer_iid,
                                       # consumed by _rewrap_pulled on the next Enter

    def _launch_inject(text):
        # Fire the fly-in as a background task (a key handler can't await):
        # _inject_or_queue owns the iid mint + the local-queue fallback.
        app = get_app()
        app.create_background_task(
            _inject_or_queue(inject, text, pending, invalidate=app.invalidate))

    def _start_turn(text):
        turn.pop("stopped", None)   # a stale stop flag must never eat the next natural drain
        turn["task"] = get_app().create_background_task(_run_turn(text))

    async def _run_turn(text):
        done = False
        try:
            await on_line(text)
            done = True
        finally:
            turn["task"] = None
            # DRAIN RULE: natural completion ONLY. A user STOP (Esc/Ctrl-C sets
            # turn["stopped"] before cancelling; repl._run_turn absorbs the
            # cancel so on_line still returns) means "I'm taking control" — the
            # queue is PRESERVED, stays visible (toolbar accent + /queue), and
            # never auto-runs; /queue clear drops it, and the next NATURAL
            # completion resumes draining. A propagating exception (dock
            # teardown) also leaves the queue be. An ERROR-terminated turn
            # (status()["turn_failed"], set by repl's except branch via
            # RichSink.mark_turn_failed — W1 front-3b) holds the queue too: a
            # broken backend must never burn one queued line per failing turn.
            stopped = turn.pop("stopped", False)
            failed = False
            try:
                failed = bool(status().get("turn_failed"))
            except Exception:
                pass
            if done and not stopped and not failed:
                # Submit the oldest queued line through the SAME path a typed
                # line takes; queued_run announces it — never a silent start.
                _drain_pending(pending, _start_turn, mark=queued_run)
            get_app().invalidate()

    kb = KeyBindings()

    def _busy_live() -> bool:
        """Lockout-proof busy for the key handlers: busy only while the turn
        TASK is genuinely alive. A turn that died without clearing the sink's
        busy flag (an error path that skipped end_turn) must never brick the
        dock -- Enter/Esc/Ctrl-C/Ctrl-D all gate on THIS, so a stale flag
        degrades to a cosmetic toolbar glitch instead of an unusable input
        (Valentin, live 2026-07-15: 'working' spun and NO key reacted)."""
        t = turn.get("task")
        return bool(is_busy() and t is not None and not t.done())

    @kb.add("enter")
    def _enter(event):
        # never send leaked mouse reports; _rewrap_pulled keeps the ORIGINAL
        # steer_iid alive when this is a pulled queued line resubmitted
        # UNCHANGED (one-shot — `pulled` is consumed either way).
        text = _rewrap_pulled(pulled, scrub_mouse_residue(buf.text))
        buf.reset()
        if consent_pending():
            resolve_consent(text)              # ICNLI: relay the raw reply verbatim
            return
        if not text.strip() and sel["i"] is not None and steps_nav and not _busy_live():
            idx, sel["i"] = sel["i"], None
            event.app.create_background_task(steps_nav["expand"](idx))
            return
        if not text.strip():
            return
        if _is_queue_command(text):
            # Queue MANAGEMENT runs NOW, even mid-turn (it never queues
            # itself): a display-only background task — the handler only
            # reads/clears the shared deque and prints, so it can't collide
            # with the live turn and never touches turn["task"].
            buf.history.append_string(text)
            event.app.create_background_task(on_line(text))
            return
        # Non-empty line: record for up-arrow recall, then fly-while-busy
        # (mid-turn inject — the line reaches the RUNNING turn within one
        # brain step; the kernel's task_queued[terminal] echo shows the panel
        # row) with local type-ahead as the no-inject/failure fallback, or
        # start a turn now (idle — unchanged).
        if _submit_line(text, buf, pending, _busy_live(), _start_turn,
                        inject=None if inject is None else _launch_inject
                        ) in ("queued", "injected"):
            event.app.invalidate()             # panel + toolbar show the new depth

    @kb.add("s-tab")
    def _cycle(event):
        on_cycle()
        event.app.invalidate()

    @kb.add("c-c")
    def _interrupt(event):
        _interrupt_action(turn, _busy_live, stop_turn, event)

    @kb.add("c-d")
    def _eof(event):
        if not _busy_live():
            event.app.exit()

    sel = {"i": None}   # None = no selection; else 0-based step index

    def _nav_count() -> int:
        try:
            return int(steps_nav["count"]()) if steps_nav else 0
        except Exception:
            return 0

    @kb.add("up")
    def _step_up(event):
        _arrow_up_action(event, buf, sel, _nav_count(), _busy_live(), pending, pulled)

    @kb.add("down")
    def _step_down(event):
        _arrow_down_action(event, buf, sel, _nav_count(), _busy_live())

    @kb.add("escape")
    def _step_clear(event):
        _escape_action(sel, turn, _busy_live, stop_turn, event, buf=buf)

    @kb.add("pageup")
    def _pgup(event):
        pane.scroll(-(max(1, pane._view_h) - 2))

    @kb.add("pagedown")
    def _pgdn(event):
        pane.scroll(max(1, pane._view_h) - 2)

    def _toolbar():
        f = pane.flash()
        if f:
            return [("class:tb.working", "  " + f)]   # transient copy confirmation
        if sel["i"] is not None and steps_nav:
            return [("class:tb.dim", f"  step {sel['i'] + 1}/{_nav_count()} · Enter to expand · Esc to cancel")]
        st = status()
        return build_toolbar(mode_getter(), st["tokens"], st["credits"], busy=st["busy"],
                             current=st["current"], elapsed=st["elapsed"],
                             tools=st["tools"], consent=st["consent"],
                             queued=len(pending) + len(remote_pending),
                             reconnecting=st.get("reconnecting", 0))

    # Dynamic height: EXACTLY the rows the wrapped input needs (1→10), so the box
    # grows as you type and shrinks back — never a fixed huge block. Enter still
    # submits (multiline=False); the pane above absorbs all remaining space.
    def _input_height():
        import shutil
        text = buf.text
        cols = max(10, shutil.get_terminal_size((100, 24)).columns - 4)  # frame + "❯ "
        if not text:
            return 1
        rows = sum(max(1, -(-len(ln) // cols)) for ln in text.split("\n"))
        return min(10, max(1, rows))

    def _prompt_fragments():
        # The ❯ takes the CURRENT mode's colour (same classes the toolbar uses)
        # so the mode is obvious from the input line itself, not just the toolbar.
        return [(f"class:tb.mode.{mode_getter()}", "❯ ")]

    def _pull_at(index: int) -> None:
        """Mouse pull (a panel row's MOUSE_UP handler, queue_panel._item_handler):
        move the CLICKED queued item into the input for editing — the SAME
        pull_item the ↑ key uses, arbitrary index instead of newest (never
        clobbers a typed draft, ignores a stale index). Mirrors _arrow_up_action:
        records the item's text + steer_iid into `pulled` so an unchanged
        resubmit keeps the original iid (see _rewrap_pulled)."""
        item = pull_item(pending, buf, index)
        if item is not None:
            pulled["text"], pulled["iid"] = str(item), getattr(item, "iid", "")
            get_app().invalidate()

    # Task 11 click-to-collapse: per-session UI state for the queue/todo
    # panels (a plain dict, not a Buffer/observable — the header's own
    # redraw-on-invalidate is the only "reactivity" either panel needs).
    # Clicking a panel's header toggles between full render and ONE row
    # ending ▸ (collapsed) / ▾ (expanded) — screen space back on demand.
    qp_ui = {"collapsed": False}
    tp_ui = {"collapsed": False}

    def _toggle_queue():
        qp_ui["collapsed"] = not qp_ui["collapsed"]
        get_app().invalidate()

    def _toggle_todos():
        tp_ui["collapsed"] = not tp_ui["collapsed"]
        get_app().invalidate()

    def _queue_fragments():
        # Live like _toolbar: re-invoked every redraw, reads the shared deque
        # + the sink-owned remote list (pull serves the LOCAL rows only —
        # remote rows are display-only by construction in queue_fragments).
        import shutil
        return queue_fragments(pending, pull=_pull_at,
                               width=shutil.get_terminal_size((100, 24)).columns,
                               remote=remote_pending,
                               collapsed=qp_ui["collapsed"], toggle=_toggle_queue)

    # The LIVE pending-queue panel — pinned BETWEEN the output pane and the
    # input box; zero rows (hidden) while the queue is empty, so the empty
    # state is pixel-identical to the panel-less dock. focusable=False keeps
    # focus on the input even when a row is clicked.
    queue_panel = ConditionalContainer(
        content=Window(FormattedTextControl(_queue_fragments, focusable=False),
                       height=lambda: queue_height(pending, remote_pending, qp_ui["collapsed"]),
                       always_hide_cursor=True, wrap_lines=False),
        filter=Condition(lambda: bool(pending) or bool(remote_pending)))

    def _todo_fragments():
        # Live like _queue_fragments: re-invoked every redraw, reads the
        # sink-owned current_todos list in place (todo frames mutate it).
        import shutil
        return todo_fragments(todos, width=shutil.get_terminal_size((100, 24)).columns,
                              collapsed=tp_ui["collapsed"], toggle=_toggle_todos)

    # The STICKY todo panel — pinned ABOVE the queue panel (the queue stays
    # adjacent to the input; its bottom row is the ↑-pullable newest). Same
    # proven stacked-ConditionalContainer pattern: zero rows while the list
    # is empty, focusable=False keeps focus on the input.
    todo_panel = ConditionalContainer(
        content=Window(FormattedTextControl(_todo_fragments, focusable=False),
                       height=lambda: todo_height(todos, tp_ui["collapsed"]),
                       always_hide_cursor=True, wrap_lines=False),
        filter=Condition(lambda: bool(todos)))

    input_win = Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput(_prompt_fragments)]),
        height=_input_height, wrap_lines=True)
    toolbar = Window(FormattedTextControl(_toolbar), height=1, always_hide_cursor=True)
    root = HSplit([pane.window, todo_panel, queue_panel, Frame(input_win), toolbar])
    style = Style.from_dict({
        "frame.border": "#5f5f5f",           # muted grey chrome — furniture, not focus
        "prompt": "#00afd7 bold",            # cyan ❯ — the interactive accent
        "tb.dim": "#8a8a8a",                 # idle chrome / secondary bits — dim
        "tb.spin": "#e8a317 bold",           # animated spinner — bee-yellow, pops
        "tb.working": "#e8a317",             # 'working' — yellow
        "tb.action": "#00afd7",              # current action — cyan
        "tb.consent": "#e8a317 bold",        # consent prompt line — yellow
        "tb.mode.default": "#00afd7",        # default — cyan
        "tb.mode.plan": "#af87ff",           # plan — purple
        "tb.mode.autopilot": "#e8a317 bold", # autopilot — yellow (auto-approving: caution)
        "qp.header": "#e8a317 bold",         # queue-panel header — bee-yellow, pops
        "qp.item": "#8a8a8a italic",         # older queued rows — muted (echoes grey66)
        "qp.last": "#e8a317",                # newest row — the one ↑ pulls
        "qp.remote": "#af87ff italic",       # cross-surface rows — purple (not yours to pull)
        "tp.header": "#e8a317 bold",         # todo-panel header — bee-yellow, pops
        "tp.done": "#5faf5f",                # ✓ glyph — green
        "tp.done.text": "#8a8a8a strike",    # completed text — dim + struck
        "tp.now": "#e8a317 bold",            # ▶ current item — bee-yellow, always pops
        "tp.item": "#8a8a8a",                # pending rows / overflow — muted
    })
    app = Application(layout=Layout(root, focused_element=input_win), key_bindings=kb,
                      full_screen=True, mouse_support=True, style=style)
    configure_mouse_modes(app.output)   # ?1002 button-event, never ?1003 any-event

    async def _ticker():
        # animate the spinner + tick the elapsed clock while a turn runs
        while True:
            await asyncio.sleep(0.25)
            if is_busy() or pane.flash():
                app.invalidate()

    tick = asyncio.ensure_future(_ticker())
    try:
        await app.run_async()
    finally:
        tick.cancel()
    return True
