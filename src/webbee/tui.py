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

from webbee import sizing
from webbee.output_pane import OutputPane  # noqa: F401 — re-exported (webbee.tui.OutputPane)
from webbee.queue_panel import pull_item, queue_fragments, queue_height
from webbee.render import _fmt_tokens
from webbee.slots import close_active, close_at, disarm_all, is_turn_alive
from webbee.tabs import tab_fragments
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
# 0.3.25: stray DEC focus-in/out reports ("\x1b[I" / "\x1b[O") — same split-
# sequence hazard as the mouse residue above, but from ANOTHER source (tmux/
# a window manager still sending them even though configure_mouse_modes now
# explicitly turns ?1004 off — see its own docstring). ESC-PREFIXED ONLY: a
# bare "[I"/"[O" with no leading ESC is ordinary text (e.g. "see [I]" in a
# citation) and must never be eaten.
_FOCUS_RESIDUE = re.compile(r"\x1b\[[IO]")


def scrub_mouse_residue(text: str) -> str:
    """PURE. Drop leaked mouse-report AND focus-report fragments (0.3.25);
    everything else unchanged."""
    text = _MOUSE_RESIDUE.sub("", text or "")
    return _FOCUS_RESIDUE.sub("", text)


_STYLE_DICT = {
    "frame.border": "#5f5f5f",           # muted grey chrome — furniture, not focus
    "prompt": "#00afd7 bold",            # cyan ❯ — the interactive accent
    "tabbar": "bg:#262626",              # 0.3.25: the bar itself — a browser-look strip the chips sit on
    "tab": "#9e9e9e",                    # idle chip — dim text, no bg (brightened a notch, 0.3.25, to read clearly on `tabbar`'s bg)
    "tab.active": "bg:#e8a317 #1c1c1c bold",  # the ACTIVE chip — solid bee-yellow bg, dark text: unmistakable
    "tab.alert": "#e8a317 bold",         # ⚠ consent waiting in a BACKGROUND tab — yellow text, no bg (only the active chip owns one); also the armed "✕?" busy-close-confirm glyph (0.3.25)
    "tab.close": "#9e9e9e",              # the ✕ on a background tab — dim, closing is never the default action (brightened alongside `tab`)
    "tab.close.active": "bg:#e8a317 #1c1c1c",  # the ✕ on the ACTIVE tab — same bg as its chip, reads as one contiguous block
    "tab.new": "#e8a317 bold",            # 0.3.26: bee-yellow + prominent (was #6f6f6f)
    "tab.sep": "#3a3a3a",                # the │ between tabs — dim, consistent, exactly one per pair, none at the ends
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
    # W5 interactive Home dashboard
    "home.header": "#e8a317 bold",
    "home.value": "#ffffff bold",
    "home.item": "#00afd7",
    "home.dim": "#8a8a8a",
    "home.disabled": "#5f5f5f",
    "home.focus": "bg:#e8a317 #1c1c1c bold",
    "home.hint": "#00afd7",
}


def configure_mouse_modes(output) -> None:
    """Replace prompt_toolkit's ANY-EVENT mouse tracking (?1003 — every bare
    mouse move fires a report) with BUTTON-EVENT tracking (?1002 — reports only
    while a button is held). Wheel scroll, clicks and drag-select all still
    work; the bare-move flood that desyncs the parser (phantom Escape + report
    tails typed into the input) disappears at the source. No-op for outputs
    without write_raw (non-vt100).

    0.3.25 (focus/garbage hardening): both paths ALSO explicitly disable
    DEC focus-reporting (?1004l) — a tmux pane switch, an OS-level window
    focus change, or another program that left ?1004 armed can otherwise
    leak `ESC[I`/`ESC[O` focus-in/out reports straight into THIS terminal's
    stdin, landing as garbage in the input buffer exactly like the mouse
    residue below. `_enable` turns it off the moment the dock's own mouse
    tracking comes up (so nothing else's focus reporting can leak for the
    whole session); `_disable` repeats it on teardown, belt & braces, same
    posture as ?1003 above."""
    if not hasattr(output, "write_raw"):
        return

    def _enable():
        output.write_raw("\x1b[?1000h")   # clicks + wheel
        output.write_raw("\x1b[?1002h")   # motion ONLY while a button is held
        output.write_raw("\x1b[?1015h")   # urxvt encoding
        output.write_raw("\x1b[?1006h")   # SGR encoding
        output.write_raw("\x1b[?1004l")   # focus reporting OFF -- never wanted here

    def _disable():
        output.write_raw("\x1b[?1002l")
        output.write_raw("\x1b[?1003l")   # belt & braces: clear any-event too
        output.write_raw("\x1b[?1000l")
        output.write_raw("\x1b[?1015l")
        output.write_raw("\x1b[?1006l")
        output.write_raw("\x1b[?1004l")   # belt & braces: focus reporting stays off on exit too

    output.enable_mouse_support = _enable
    output.disable_mouse_support = _disable


def input_rows(text: str, cols: int, cap: int) -> int:
    """PURE row-wrap estimator behind `_input_height` (module-level so tests
    drive it directly with an injected size, mirroring repl._gate_busy).
    Same wrap math as before the W2 proportional-sizing pass: `cols` is the
    usable wrap width (frame + prompt already subtracted by the caller,
    floored at 10 so a tiny/misreported width never collapses every line to
    1-char rows); `cap` bounds growth (was: hardcoded 10, now the caller's
    live `sizing.input_height_cap(rows)` — the box may grow to at most a
    PROPORTION of the screen, not a fixed character count)."""
    if not text:
        return 1
    cols = max(10, cols)
    rows = sum(max(1, -(-len(ln) // cols)) for ln in text.split("\n"))
    return min(cap, max(1, rows))


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


def _width_watch(pane, app) -> None:
    """Per-tick resize detector (W2 front-2): prompt_toolkit repaints on
    SIGWINCH by itself, but the RICH side (console width) must be told to
    re-wrap — this is the bridge. Two int compares when nothing changed.
    Swallows any reflow error — the ticker is the dock's only animation
    loop (spinner + queue drains ride on it too) and must never die."""
    from webbee.sizing import get_size
    cols, _rows = get_size(app)
    if cols and cols != pane.console.width:
        try:
            pane.reflow(cols)
        except Exception:
            pass


def _tick_once(slots, app, is_busy) -> None:
    """One iteration of run_session's `_ticker` loop, extracted module-level
    so the wiring itself is directly unit-testable (an `async def` infinite
    loop otherwise only proves itself by running the whole dock). W4a Task 3:
    takes the SlotManager, not a pane — `slots.active().pane` is resolved
    HERE, every tick, so a tab switch immediately redirects the ticker at the
    newly-visible slot's own pane (its edge-drag, its resize-reflow) with no
    stale reference left over from the slot that was active a moment ago.
    Three effects, in order: (1) `_width_watch` — resize-detect + reflow
    bridge, UNCONDITIONAL busy or idle; (2) `pane.edge_tick()` — repeat-scroll
    while parked at a drag edge, error-swallowed so a broken edge-tick can
    never kill the dock's only animation loop; (3) `app.invalidate()` exactly
    when a turn is running OR the copy-flash toast is still fresh, so the
    spinner/elapsed-clock/flash-expiry all animate without redrawing on every
    idle tick for nothing."""
    pane = slots.active().pane
    _width_watch(pane, app)
    try:
        pane.edge_tick()
    except Exception:
        pass
    if is_busy() or pane.flash():
        app.invalidate()


def _forwarding(handler, pane):
    """W2 Task 8: prompt_toolkit routes mouse events by pointer POSITION,
    not by who owns an in-progress drag, so a selection armed inside the
    output pane needs its neighbor windows' own mouse handling to give it
    first refusal — otherwise a release past the pane's Window just lands on
    whatever's underneath and the drag never completes (stuck highlight,
    copy never fires). Wraps `handler` (a plain mouse_handler(ev), or None
    for a window that has no handler of its own — e.g. the toolbar) so
    `pane.forward_mouse(ev)` is tried FIRST: consumed (a drag was armed) ⇒
    stop here, return None; otherwise fall through to `handler(ev)`, or
    NotImplemented when there's no wrapped handler at all — the toolbar's
    case, where forwarding is the ONLY behavior being added."""
    def _h(ev):
        if pane.forward_mouse(ev):
            return None
        if handler is None:
            return NotImplemented
        return handler(ev)
    return _h


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


def _can_close_tab(buf, slot) -> bool:
    """PURE. Ctrl-W's filter predicate (Task 5) — same DI-testing philosophy
    as `_escape_action`/`_interrupt_action`, exposed module-level so a test
    drives it directly instead of only through a live Application. Ctrl-W
    closes the active tab ONLY when the input is empty (a non-empty draft
    means the user wants PT's normal word-delete, not a tab close) AND the
    active slot is an actual session (Home has nothing to close)."""
    return not buf.text and slot is not None and slot.kind == "session"


def _should_close_on_eof(slots) -> bool:
    """PURE. Ctrl-D's tab-vs-quit policy (Task 5): closing the active SESSION
    tab is the natural Ctrl-D action as long as at least one OTHER session
    tab survives — closing the last one instead falls through to the
    original behavior (exit the app when idle), since landing on a bare Home
    with nothing left open is close enough to "quit" that a second Ctrl-D
    finishes the job instead of a tab-close silently doing nothing new."""
    return slots.session_count() > 1 and slots.active().kind == "session"


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


def _swap_history(buf, slot) -> None:
    """Per-slot input-history plumbing (W4a Task 3 — Task 5 wires it to an
    actual tab switch): the dock keeps ONE shared Buffer (one input box,
    unchanged), but each slot should recall (↑ readline-style) only the
    lines IT submitted — so a slot's OWN `InMemoryHistory` is minted on
    first touch (stored on `slot.history`) and the shared buffer is
    re-pointed at it. A repeat call for the SAME slot reuses its existing
    history verbatim (never mints a second one, never loses recall)."""
    from prompt_toolkit.history import InMemoryHistory
    if slot.history is None:
        slot.history = InMemoryHistory()
    buf.history = slot.history


def _restore_draft(buf, slot) -> None:
    """0.3.24 (per-tab drafts — the browser-tab model: each tab keeps its own
    form state): `buf.reset()` must run FIRST (it clears the history-load
    task state -- unchanged from before this fix), THEN the buffer's live
    text/cursor are set from `slot`'s OWN stashed draft -- restoring exactly
    what was mid-type in THIS tab the last time it was left, never another
    tab's. `min(...)` guards a shrunk/replaced draft never leaving the
    cursor past the end of the text it's now landing on."""
    buf.reset()
    buf.text = slot.draft
    buf.cursor_position = min(slot.draft_cursor, len(buf.text))


async def run_session(*, slots, on_line, on_cycle, steps_nav=None,
                      stop_turn=None, queued_run=None, inject=None,
                      home_input=None, cancel_slot=None, ui_hooks=None,
                      on_switch=None, on_new=None) -> bool:
    """The full-screen dock: EVERYTHING visible resolves `slots.active()` AT
    CALL TIME (W4a Task 3 — the single most structural change of the
    multisession-tabs wave: no more one session's objects captured once at
    the top). `slots.active().pane` fills the top (a `DynamicContainer`, so
    it re-resolves on every redraw — a tab switch repaints a different
    slot's transcript with zero stale references); a bordered input box +
    toolbar are FIXED at the bottom, shared by every tab (one Buffer — see
    `_swap_history`, wired in a later task). Enter either resolves a pending
    consent reply on the ACTIVE slot's sink (ICNLI: raw verbatim) or starts a
    turn as a BACKGROUND task PINNED to the slot it started in
    (`_start_turn_in` captures that slot ONCE — its own turn dict, queue
    drain, and turn-failed read all stay targeted at it even if the user
    switches tabs while it runs; every OTHER read in this function — Esc/
    Ctrl-C, the toolbar, the queue/todo panels, ↑/click-pull — always acts on
    whatever slot is VISIBLE right now). While a turn runs, Enter on that
    SAME slot queues the line (type-ahead — the LIVE queue panel above the
    input shows it and the toolbar shows `⋯N queued` in accent) or flies it
    into the running turn via `inject` (mid-turn inject, 0.3.15) — both keyed
    off the active slot's own `pending`/`turn`/`pulled`/`qp_ui`/`tp_ui`
    (`SessionSlot` fields, not this function's own locals anymore). A Home
    slot (`sink=None`) has no busy/consent/queue/todo state at all — the
    `_sink_attr` accessor's `default` covers every read; Enter with a
    non-command line on Home calls `home_input(text)` when the caller wired
    one (Task 6 — `None` means ignored), while a slash command on Home still
    reaches `on_line`, same as every other slot. `steps_nav`/`stop_turn`/
    `inject`/`on_cycle`/`queued_run` are INJECTED callables the repl already
    resolves through `slots.active()` itself before handing them here — this
    function only calls them, it never reaches into a session object through
    them. A tab bar (Task 4, `webbee.tabs.tab_fragments`) is pinned at the
    very top of the dock, ALWAYS visible: a click switches tabs (`_switch_to`
    — a no-op on the already-active tab or a stale idx, since
    `slots.switch` already guards both); a session tab's ✕ closes THAT tab
    (`_close_tab_click(idx)` -> `webbee.slots.close_at(slots, idx,
    cancel_slot)`, Task 7 -- the clicked idx, not necessarily whichever tab
    is active), while Ctrl-W AND Ctrl-D-with-other-tabs-open have no per-tab
    idx of their own and keep reaching `_close_flow` -> `close_active(slots,
    cancel_slot)` (Task 5) — always "close what I'm looking at". Both PT-free
    functions live in `webbee.slots` (`close_active` is a thin wrapper over
    `close_at`), shared verbatim with repl's `/close` command. `cancel_slot`
    (a NEW repl-injected callable) tears down the removed slot's OWN
    background tasks; the kernel's run keeps going server-side regardless
    (browser-tab model). `Ctrl-T` and `Alt+0..9` (prompt_toolkit sees the
    latter as the two-key sequence `("escape", "<digit>")`) both land on
    `_switch_to` — the bare `escape` binding (stop-turn / step-clear) stays
    registered too; prompt_toolkit's own key-processor timeout disambiguates
    a lone Escape from an Escape-then-digit chord, same mechanism its own
    default emacs bindings already lean on for `escape,f`/`escape,b`/etc —
    `app.timeoutlen` is turned down well below its 1.0s default (see below)
    so a genuine lone Escape still resolves quickly instead of only your
    patience finding out. `ui_hooks` (optional, repl-owned mutable dict):
    this function fills `ui_hooks["switch"] = _switch_to` and
    `ui_hooks["close"] = _close_flow` at construction time, so repl's
    `/tab`/`/new`/`/close` commands route through the EXACT same switch/close
    path the keys and clicks use (the history swap on every switch, the
    close note) instead of mutating
    `slots` directly and missing it. `on_switch` (Task 6, optional): called
    with the NEW active idx after every successful `_switch_to` -- click,
    Ctrl-T, Alt+N, or a repl command via `ui_hooks["switch"]` all converge
    on `_switch_to`, so this one seam covers every path. repl wires it to
    its own stale-Home-refill check (`home.is_stale` + `fill_home`
    re-scheduled as a bg task) -- this function has no idea what "Home" or
    "stale" mean, it only calls the hook. `on_new` (0.3.25, optional): the
    tab bar's trailing + chip fires this — a bare async callable, no args,
    fired as a background task (`_new_tab_click`, same "can't await from a
    mouse handler" shape as `_launch_inject`) — repl wires it to the EXACT
    same flow `/new` (no arg) uses, which itself calls `ui_hooks["switch"]`
    to land on the new tab, so the history/draft swap always runs through
    `_switch_to` too, never bypassed. `None` (the default, and every test
    that doesn't care) makes a + click a harmless no-op via `tabs.
    tab_fragments`'s own contract.

    Busy-close confirm (Part D): a ✕ click on a tab whose OWN turn task is
    still alive (`slots.is_turn_alive`) arms `slot.close_armed` instead of
    closing outright — `_close_tab_click` below — and a note lands in that
    tab's own transcript; the tab bar then renders "✕?" (`tabs.
    tab_fragments`) until either a second click on the SAME armed tab
    actually closes it, or ANY switch/keypress disarms it again
    (`slots.disarm_all`, wired into `_switch_to` and the Application's
    `after_key_press` event below).
    Returns True on clean exit; False if prompt_toolkit is unavailable
    (the caller uses the plain fallback loop)."""
    try:
        from prompt_toolkit.application import Application, get_app, get_app_or_none
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer,
                                          HSplit, Layout, Window)
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except Exception:
        return False

    def _a():
        return slots.active()

    def _pane():
        return _a().pane

    def _sink_attr(name, default=None):
        s = _a().sink
        return getattr(s, name, default) if s is not None else default

    buf = Buffer(multiline=False)

    def _launch_inject(text):
        # Fire the fly-in as a background task (a key handler can't await):
        # _inject_or_queue owns the iid mint + the local-queue fallback.
        # `slot` is captured HERE, SYNCHRONOUSLY, at Enter keypress time
        # (FIX7a — mirrors the turn-start pinning everywhere else in this
        # module) -- both the target deque AND the inject POST itself stay
        # pinned to THIS slot even if the user switches tabs before the
        # scheduled background task's body actually runs. `inject` itself
        # keeps its existing 2-arg (text, iid) contract as far as
        # `_inject_or_queue` is concerned -- the slot rides along in a thin
        # wrapper closure, so `_inject_or_queue`'s own signature/tests need
        # no change at all.
        slot = _a()
        app = get_app()
        app.create_background_task(
            _inject_or_queue(lambda t, i: inject(t, i, slot), text, slot.pending,
                             invalidate=app.invalidate))

    def _start_turn_in(slot, text):
        slot.turn.pop("stopped", None)   # a stale stop flag must never eat the next natural drain
        slot.turn["task"] = get_app().create_background_task(_run_turn(slot, text))

    def _start_turn(text):
        _start_turn_in(_a(), text)

    def _finish_natural_turn(slot, done: bool) -> None:
        """Shared finally-tail of ONE turn (typed via `_run_turn` below, OR
        an attach pickup via `_start_attach_in`'s wrapper) -- clears
        `slot.turn["task"]`, then the DRAIN RULE: natural completion ONLY.
        A user STOP (Esc/Ctrl-C sets turn["stopped"] before cancelling; the
        caller absorbs the cancel so `done` stays False) means "I'm taking
        control" — the queue is PRESERVED, stays visible (toolbar accent +
        /queue), and never auto-runs; /queue clear drops it, and the next
        NATURAL completion resumes draining. A propagating exception (dock
        teardown) also leaves the queue be. An ERROR-terminated turn
        (slot.sink.status()["turn_failed"], set by repl's except branch via
        RichSink.mark_turn_failed — W1 front-3b) holds the queue too: a
        broken backend must never burn one queued line per failing turn.
        Factored out of `_run_turn` so an attach pickup gets the EXACT SAME
        queue-drain/stop/failed semantics a typed turn has, instead of a
        parallel half-copy that silently drifts."""
        slot.turn["task"] = None
        stopped = slot.turn.pop("stopped", False)
        failed = False
        try:
            failed = bool(slot.sink.status().get("turn_failed"))
        except Exception:
            pass
        if done and not stopped and not failed:
            # Submit the oldest queued line through the SAME path a typed
            # line takes, back into the SAME (pinned) slot; queued_run
            # announces it — never a silent start.
            _drain_pending(slot.pending, lambda t: _start_turn_in(slot, t), mark=queued_run)
        get_app().invalidate()

    def _start_attach_in(slot, coro):
        """Attach-on-poll's own start-turn seam (`ui_hooks["start_attach_in"]`,
        registered below) -- mirrors `_start_turn_in`'s task-tracking so Esc/
        Ctrl-C can cancel an in-flight attach turn exactly like a typed one
        (both key handlers cancel `slot.turn.get("task")` directly), and
        `_finish_natural_turn` above so its queue-drain/stop/failed tail is
        identical too. There is no `on_line`/text path for an attach (the
        caller -- repl's `poll_idle_steer` `attach_turn` seam -- already
        built the whole turn's coroutine, `sink.begin_turn()` through
        `end_turn()`), so this takes a ready-made coroutine rather than
        routing through `_run_turn`; it returns the spawned task so the
        caller can await it itself (the poller must block for the turn's
        whole duration -- unlike a typed Enter, which is fire-and-forget
        from a key handler)."""
        async def _wrapped():
            done = False
            try:
                await coro
                done = True
            finally:
                _finish_natural_turn(slot, done)
        slot.turn.pop("stopped", None)
        slot.turn["task"] = get_app().create_background_task(_wrapped())
        return slot.turn["task"]

    async def _run_turn(slot, text):
        # `slot` is bound ONCE by the caller (Enter's idle path, or a drain
        # re-submitting into the SAME slot it drained from) -- a turn belongs
        # to the slot it started in. Every read/mutation below stays pinned
        # to THAT slot even if the user switches tabs while it runs; this is
        # the one deliberate exception to "always resolve active() at call
        # time" everywhere else in this function.
        done = False
        try:
            await on_line(text, slot)
            done = True
        finally:
            _finish_natural_turn(slot, done)

    kb = KeyBindings()

    def _busy_live() -> bool:
        """Lockout-proof busy for the key handlers, on the ACTIVE slot: busy
        only while ITS turn TASK is genuinely alive. A turn that died without
        clearing the sink's busy flag (an error path that skipped end_turn)
        must never brick the dock -- Enter/Esc/Ctrl-C all gate on THIS, so a
        stale flag degrades to a cosmetic toolbar glitch instead of an
        unusable input (Valentin, live 2026-07-15: 'working' spun and NO key
        reacted). A Home slot (no sink) is never busy. Thin wrapper over the
        per-slot `_slot_busy` (FIX5) -- Ctrl-D's `_eof` uses THAT directly,
        across every slot, not just the active one."""
        return _slot_busy(_a())

    def _slot_busy(slot) -> bool:
        """Lockout-proof busy for an ARBITRARY slot (FIX5, generalizes
        `_busy_live` above -- same predicate, parameterized): busy only
        while ITS OWN turn TASK is genuinely alive AND its sink reports
        busy. Ctrl-D's `_eof` needs this across EVERY slot, not just the
        active one -- a background tab's live turn must never let a Ctrl-D
        pressed on Home (or any other idle tab) exit right through it."""
        t = slot.turn.get("task")
        sink = slot.sink
        ib = getattr(sink, "is_busy", None) if sink is not None else None
        busy = bool(ib()) if callable(ib) else False
        return bool(busy and t is not None and not t.done())

    @kb.add("enter")
    def _enter(event):
        # never send leaked mouse reports; _rewrap_pulled keeps the ORIGINAL
        # steer_iid alive when this is a pulled queued line resubmitted
        # UNCHANGED (one-shot — the ACTIVE slot's `pulled` is consumed
        # either way).
        slot = _a()
        text = _rewrap_pulled(slot.pulled, scrub_mouse_residue(buf.text))
        buf.reset()
        # 0.3.24: a genuine Enter retires THIS slot's own stashed draft too
        # -- otherwise the NEXT switch-away-then-back (nothing typed in
        # between) would restore text that was already sent, resurrecting a
        # message the user believes is long gone.
        slot.draft = ""
        slot.draft_cursor = 0
        if slot.kind == "home" and not text.strip():
            # Empty Enter on the interactive Home activates the focused item.
            slot.pane.activate_focused()
            return
        cp = _sink_attr("consent_pending")
        if cp and cp():
            rc = _sink_attr("resolve_consent")
            if rc:
                rc(text)                       # ICNLI: relay the raw reply verbatim
            return
        if not text.strip() and sel["i"] is not None and steps_nav and not _busy_live():
            idx, sel["i"] = sel["i"], None
            event.app.create_background_task(steps_nav["expand"](idx, slot))
            return
        if not text.strip():
            return
        if _is_queue_command(text):
            # Queue MANAGEMENT runs NOW, even mid-turn (it never queues
            # itself): a display-only background task — the handler only
            # reads/clears the shared deque and prints, so it can't collide
            # with the live turn and never touches turn["task"]. `slot` is
            # the ACTIVE-AT-KEYPRESS slot captured above (FIX1) — the SAME
            # slot on_line acts against no matter what becomes active
            # before this scheduled task's body actually runs.
            buf.history.append_string(text)
            event.app.create_background_task(on_line(text, slot))
            return
        if slot.sink is None:
            # Home: no busy/queue/inject semantics at all. A slash command
            # still reaches on_line exactly like every other slot (against
            # the Home slot itself, FIX1); plain text is the caller's own
            # affair (Task 6 wires home_input -- None here simply means
            # "ignored").
            buf.history.append_string(text)
            if text.strip().startswith("/"):
                event.app.create_background_task(on_line(text, slot))
            elif home_input is not None:
                event.app.create_background_task(home_input(text))
            return
        # Non-empty line: record for up-arrow recall, then fly-while-busy
        # (mid-turn inject — the line reaches the RUNNING turn within one
        # brain step; the kernel's task_queued[terminal] echo shows the panel
        # row) with local type-ahead as the no-inject/failure fallback, or
        # start a turn now (idle — unchanged).
        if _submit_line(text, buf, slot.pending, _busy_live(), _start_turn,
                        inject=None if inject is None else _launch_inject
                        ) in ("queued", "injected"):
            event.app.invalidate()             # panel + toolbar show the new depth

    @kb.add("s-tab")
    def _cycle(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.focus_prev()
            event.app.invalidate()
            return
        on_cycle()
        event.app.invalidate()

    @kb.add("c-c")
    def _interrupt(event):
        _interrupt_action(_a().turn, _busy_live, stop_turn, event)

    @kb.add("c-t")
    def _new_tab_key(event):
        # 0.3.26: Ctrl-T opens a NEW tab (the browser gesture), via the exact
        # seam the tab bar's + chip uses (`_new_tab_click` -> on_new ->
        # repl._open_new_tab). Home stays reachable by clicking its ◆ chip or
        # Alt+1-style switch (footer legend reminds muscle-memory users).
        _new_tab_click()

    def _alt_digit_handler(d: int):
        def _h(event):
            _switch_to(d)
        return _h

    for _d in range(10):
        # Alt+N == prompt_toolkit's two-key sequence ("escape", "<digit>") —
        # the SAME mechanism its own default emacs bindings use for
        # escape+f/escape+b/etc, coexisting with the plain "escape" binding
        # below (stop-turn / step-clear) via the key-processor's own
        # prefix-of-longer-match timeout, tuned down further below.
        kb.add("escape", str(_d))(_alt_digit_handler(_d))

    @kb.add("c-w", filter=Condition(lambda: _can_close_tab(buf, _a())))
    def _close_tab_key(event):
        # Filtered, not unconditional (contract): an empty input on an
        # active SESSION tab closes it; any OTHER state (draft text present,
        # or Home active) leaves this binding's filter False, so the match
        # falls through to prompt_toolkit's own default emacs/basic Ctrl-W
        # (unix-word-rubout) untouched.
        _close_flow()

    @kb.add("c-d")
    def _eof(event):
        # Ctrl-D policy (Task 5, generalized FIX5): closing the active
        # SESSION tab is the natural action as long as another session tab
        # survives it; otherwise a running turn must never be torn down by
        # a stray EOF -- but FIX5 widens that guard past the ACTIVE slot:
        # a background tab's live turn (e.g. Home-spawned via _home_input,
        # or any tab left running while you switched away) must ALSO block
        # exit, not just whichever slot happens to be visible right now.
        if _should_close_on_eof(slots):
            _close_flow()
            return
        if any(_slot_busy(s) for s in slots.slots):
            return
        event.app.exit()

    sel = {"i": None}   # None = no selection; else 0-based step index

    def _nav_count() -> int:
        try:
            return int(steps_nav["count"]()) if steps_nav else 0
        except Exception:
            return 0

    @kb.add("up")
    def _step_up(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.move_focus(-1)
            return
        _arrow_up_action(event, buf, sel, _nav_count(), _busy_live(), slot.pending, slot.pulled)

    @kb.add("down")
    def _step_down(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.move_focus(1)
            return
        _arrow_down_action(event, buf, sel, _nav_count(), _busy_live())

    @kb.add("escape")
    def _step_clear(event):
        _escape_action(sel, _a().turn, _busy_live, stop_turn, event, buf=buf)

    _home_nav = Condition(lambda: _a().kind == "home" and not buf.text)

    @kb.add("tab", filter=_home_nav)
    def _home_focus_next(event):
        _a().pane.focus_next()

    @kb.add("left", filter=_home_nav)
    def _home_seg_left(event):
        _a().pane.seg_left()

    @kb.add("right", filter=_home_nav)
    def _home_seg_right(event):
        _a().pane.seg_right()

    @kb.add("pageup")
    def _pgup(event):
        pane = _pane()
        pane.scroll(-(max(1, pane._view_h) - 2))

    @kb.add("pagedown")
    def _pgdn(event):
        pane = _pane()
        pane.scroll(max(1, pane._view_h) - 2)

    def _toolbar():
        pane = _pane()
        f = pane.flash()
        if f:
            frags = [("class:tb.working", "  " + f)]   # transient copy confirmation
        elif sel["i"] is not None and steps_nav:
            frags = [("class:tb.dim", f"  step {sel['i'] + 1}/{_nav_count()} · Enter to expand · Esc to cancel")]
        else:
            st_fn = _sink_attr("status")
            if callable(st_fn):
                slot = _a()
                st = st_fn()
                frags = build_toolbar(slot.mode, st["tokens"], st["credits"], busy=st["busy"],
                                      current=st["current"], elapsed=st["elapsed"],
                                      tools=st["tools"], consent=st["consent"],
                                      queued=len(slot.pending) + len(_sink_attr("remote_pending", [])),
                                      reconnecting=st.get("reconnecting", 0))
            else:
                # Home has no sink -- an idle toolbar of its own, no tokens/
                # credits/mode to show (there's no session yet).
                frags = [("class:tb.dim", "  ◆ home · type to start a session · Alt+№ switch")]
        # W2 Task 8: the toolbar has no mouse handling of its own, so
        # `_forwarding(None, pane)` is wrapped onto every fragment purely for
        # drag-forwarding — a release that lands on the toolbar row while a
        # pane selection is armed still completes the copy instead of
        # sticking. `build_toolbar` itself stays untouched/2-tuple (its own
        # unit tests unpack `for _, seg in frags`).
        fwd = _forwarding(None, pane)
        return [(style, text, fwd) for style, text in frags]

    # Dynamic height: EXACTLY the rows the wrapped input needs (1→cap), so the
    # box grows as you type and shrinks back — never a fixed huge block. Enter
    # still submits (multiline=False); the pane above absorbs all remaining
    # space. Live size (W2 front-2, proportions not pixels): the wrap width
    # comes from the input window's OWN render_info once it has rendered at
    # least once (the true columns inside the frame, after the "❯ " prompt
    # and any margin) — `cols - 4` is the pre-first-render/headless fallback
    # the old shutil-based estimate used; the cap is a PROPORTION of the
    # live rows (sizing.input_height_cap), not a fixed 10.
    def _input_height():
        cols, rows = sizing.get_size(get_app_or_none())
        ri = getattr(input_win, "render_info", None)
        width = getattr(ri, "window_width", None) if ri is not None else None
        return input_rows(buf.text, width or (cols - 4), sizing.input_height_cap(rows))

    def _prompt_fragments():
        # The ❯ takes the CURRENT mode's colour (same classes the toolbar uses)
        # so the mode is obvious from the input line itself, not just the toolbar.
        return [(f"class:tb.mode.{_a().mode}", "❯ ")]

    def _pull_at(index: int) -> None:
        """Mouse pull (a panel row's MOUSE_UP handler, queue_panel._item_handler):
        move the CLICKED queued item into the input for editing — the SAME
        pull_item the ↑ key uses, arbitrary index instead of newest (never
        clobbers a typed draft, ignores a stale index). Mirrors _arrow_up_action:
        records the item's text + steer_iid into the ACTIVE slot's `pulled`
        so an unchanged resubmit keeps the original iid (see _rewrap_pulled)."""
        slot = _a()
        item = pull_item(slot.pending, buf, index)
        if item is not None:
            slot.pulled["text"], slot.pulled["iid"] = str(item), getattr(item, "iid", "")
            get_app().invalidate()

    def _panel_size(floor: int):
        """(cols, item-row cap) shared by a panel's fragment builder AND its
        ConditionalContainer height lambda — ONE size read so the rendered
        rows and the reserved height can never disagree (W2 front-2:
        proportions, not pixels — was the fixed QP/TP_MAX_ITEMS). `floor` is
        each panel's own today's-look constant (queue=5, todo=6) passed
        through to sizing.panel_cap so a normal 24-row terminal keeps its
        pre-W2 row count and only a tall screen grows past it."""
        cols, rows = sizing.get_size(get_app_or_none())
        return cols, sizing.panel_cap(rows, floor)

    def _toggle_queue():
        slot = _a()
        slot.qp_ui["collapsed"] = not slot.qp_ui["collapsed"]
        get_app().invalidate()

    def _toggle_todos():
        slot = _a()
        slot.tp_ui["collapsed"] = not slot.tp_ui["collapsed"]
        get_app().invalidate()

    def _queue_fragments():
        # Live like _toolbar: re-invoked every redraw, reads the ACTIVE
        # slot's own deque + the sink-owned remote list (pull serves the
        # LOCAL rows only — remote rows are display-only by construction in
        # queue_fragments). forward=pane.forward_mouse (W2 Task 8): first
        # refusal on every row/header click so a drag armed on the pane
        # above can still be extended/completed once it releases here.
        slot = _a()
        cols, cap = _panel_size(5)
        return queue_fragments(slot.pending, pull=_pull_at, width=cols,
                               remote=_sink_attr("remote_pending", []),
                               collapsed=slot.qp_ui["collapsed"],
                               toggle=_toggle_queue, max_items=cap,
                               forward=_pane().forward_mouse)

    # The LIVE pending-queue panel — pinned BETWEEN the output pane and the
    # input box; zero rows (hidden) while the queue is empty, so the empty
    # state is pixel-identical to the panel-less dock. focusable=False keeps
    # focus on the input even when a row is clicked.
    queue_panel = ConditionalContainer(
        content=Window(FormattedTextControl(_queue_fragments, focusable=False),
                       height=lambda: queue_height(_a().pending, _sink_attr("remote_pending", []),
                                                   _a().qp_ui["collapsed"],
                                                   max_items=_panel_size(5)[1]),
                       always_hide_cursor=True, wrap_lines=False),
        filter=Condition(lambda: bool(_a().pending) or bool(_sink_attr("remote_pending", []))))

    def _todo_fragments():
        # Live like _queue_fragments: re-invoked every redraw, reads the
        # ACTIVE slot's sink-owned current_todos list in place (todo frames
        # mutate it). forward=pane.forward_mouse (W2 Task 8): same
        # first-refusal seam.
        slot = _a()
        cols, cap = _panel_size(6)
        return todo_fragments(_sink_attr("current_todos", []), width=cols,
                              collapsed=slot.tp_ui["collapsed"],
                              toggle=_toggle_todos, max_items=cap,
                              forward=_pane().forward_mouse)

    # The STICKY todo panel — pinned ABOVE the queue panel (the queue stays
    # adjacent to the input; its bottom row is the ↑-pullable newest). Same
    # proven stacked-ConditionalContainer pattern: zero rows while the list
    # is empty, focusable=False keeps focus on the input.
    todo_panel = ConditionalContainer(
        content=Window(FormattedTextControl(_todo_fragments, focusable=False),
                       height=lambda: todo_height(_sink_attr("current_todos", []),
                                                  _a().tp_ui["collapsed"],
                                                  max_items=_panel_size(6)[1]),
                       always_hide_cursor=True, wrap_lines=False),
        filter=Condition(lambda: bool(_sink_attr("current_todos", []))))

    input_win = Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput(_prompt_fragments)]),
        height=_input_height, wrap_lines=True)
    toolbar = Window(FormattedTextControl(_toolbar), height=1, always_hide_cursor=True)

    _hover_on = {"v": None}

    def _sync_hover_mode() -> None:
        # ?1003 (any-event mouse = hover) ONLY while Home is active; restore
        # ?1002 (button-event) on leave. Idempotent: writes only on a state
        # change. Teardown's own ?1003l (configure_mouse_modes._disable) is
        # the belt-and-braces cleanup on exit.
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
        if app is None:
            return
        want = (_a().kind == "home")
        if _hover_on["v"] == want:
            return
        out = app.output
        if not hasattr(out, "write_raw"):
            _hover_on["v"] = want
            return
        try:
            if want:
                out.write_raw("\x1b[?1003h")
            else:
                out.write_raw("\x1b[?1003l")
                out.write_raw("\x1b[?1002h")
            out.flush()
        except Exception:
            pass
        _hover_on["v"] = want

    def _switch_to(idx: int) -> None:
        # Tab-bar click -> switch tabs. `slots.switch` already guards a
        # no-op (the clicked tab is already active) and a stale idx (the
        # tab closed between render and release) by returning False -- when
        # it does, neither the history swap nor the redraw happen, so a
        # click on the active tab is a true no-op, never a crash. `prev`
        # captured BEFORE the switch (FIX7b) -- it's the slot we're LEAVING.
        # 0.3.24: stashed UNCONDITIONALLY, before the switch even resolves --
        # harmless on a no-op switch (prev IS the still-active slot, so this
        # is just re-saving its own current text over itself).
        prev = slots.active()
        prev.draft = buf.text
        prev.draft_cursor = buf.cursor_position
        if slots.switch(idx):
            entering = slots.active()
            # Part D: any genuine switch disarms every tab's one-shot
            # busy-close confirm -- an armed "✕?" left over from a click on
            # a DIFFERENT tab (or this one, abandoned) must never linger
            # past the moment the user looks elsewhere.
            disarm_all(slots)
            _swap_history(buf, entering)
            # 0.3.24 (per-tab drafts, product decision -- was FIX7b's "drafts
            # dropped on switch"): a draft mid-type belongs to the tab you
            # typed it into, browser-tab style -- switching away no longer
            # destroys it, it comes right back when you switch back to THIS
            # tab (`_restore_draft`, which still runs the history-load
            # `buf.reset()` first). The leaving slot's own pulled-queue-item
            # carry (↑ pull-to-edit, see _rewrap_pulled) is no longer cleared
            # here either -- it now travels WITH the draft on its own slot,
            # so resubmitting it unedited after a round trip still dedups
            # correctly against a landed twin; `_rewrap_pulled`'s one-shot
            # consume (on the Enter that actually resubmits, in whichever
            # slot is active then) is what retires it, not a switch.
            _restore_draft(buf, entering)
            if on_switch is not None:
                on_switch(idx)
            get_app().invalidate()
            _sync_hover_mode()

    def _close_flow() -> bool:
        # The REAL close flow (Task 5): delegates to webbee.slots.close_active
        # (Home guard, active-idx adjustment, cancel_slot, the post-close
        # note — all PT-free and shared verbatim with repl's `/close`), then
        # invalidates on a genuine close so the tab bar/pane repaint at once.
        # FIX7d: the SURVIVOR (post-close active) slot's own history takes
        # over the shared input buffer, exactly like any other switch — a
        # closed tab's history dies with it, so the buffer must never keep
        # pointing at it. 0.3.24: the buffer now loads the SURVIVOR's own
        # draft (same `_restore_draft` a plain switch uses) instead of a
        # bare reset -- the closed tab's own draft is simply gone with it,
        # nothing to stash (it's not coming back).
        if close_active(slots, cancel_slot):
            survivor = slots.active()
            _swap_history(buf, survivor)
            _restore_draft(buf, survivor)
            get_app().invalidate()
            return True
        return False

    def _close_tab_click(idx: int) -> bool:
        # Task 7 hygiene fix (was: "honest v1" -- clicking ANY ✕ closed the
        # CURRENTLY ACTIVE tab, ignoring which one was actually clicked).
        # `close_at` already resolves the correct post-close active_idx no
        # matter which slot disappears -- the clicked tab itself, one before
        # the active tab, or one after it -- so this needs no branching.
        # Ctrl-W/Ctrl-D/the /close command are UNCHANGED: they have no
        # per-tab idx of their own, so they keep meaning "close what I'm
        # looking at" via `_close_flow`/`close_active` below.
        #
        # Part D (busy-close confirm): a ✕ click on a tab whose OWN turn is
        # still running arms `close_armed` instead of closing outright --
        # the tab bar then renders "✕?" (tabs.tab_fragments) -- and a note
        # lands in THAT tab's own transcript so it's obvious what just
        # happened even if the click landed on a BACKGROUND tab you weren't
        # even looking at. A SECOND click while already armed falls through
        # to the real close below, same as an idle tab's very first click.
        if 0 <= idx < len(slots.slots):
            target = slots.slots[idx]
            if is_turn_alive(target) and not target.close_armed:
                target.close_armed = True
                note = getattr(target.sink, "note", None)
                if note is not None:
                    note("tab is busy — click ✕ again to close (the server-side run keeps going)")
                get_app().invalidate()
                return False
        if close_at(slots, idx, cancel_slot):
            survivor = slots.active()
            _swap_history(buf, survivor)         # FIX7d, same as _close_flow above
            _restore_draft(buf, survivor)        # 0.3.24, same as _close_flow above
            get_app().invalidate()
            return True
        return False

    def _new_tab_click() -> None:
        # 0.3.25: the tab bar's + chip -- mirrors `_launch_inject`'s "a mouse
        # handler can't await" shape (fire-and-forget as a background task).
        # `on_new` is the repl's own `_open_new_tab` (async, no args); `None`
        # (no seam wired -- headless/no-dock callers, tests that don't care)
        # is a harmless no-op, same contract `tabs.tab_fragments` already
        # documents for a bare click with nothing wired.
        if on_new is None:
            return
        get_app().create_background_task(on_new())

    # repl-owned hook seam (Task 5, map contract item 5): `/tab`, `/new` and
    # `/close` live in repl.py and only ever mutate `slots` directly -- filled
    # in here so they route through the EXACT same switch/close path as a
    # click or a key (the history swap on every switch, the close note),
    # instead of quietly bypassing it. `ui_hooks=None` (headless/no-dock
    # callers, and every existing test that doesn't pass one) leaves repl's
    # own `ui_hooks.get("switch", slots.switch)` fallback in charge.
    if ui_hooks is not None:
        ui_hooks["switch"] = _switch_to
        ui_hooks["close"] = _close_flow
        # FIX3: the Home-spawned first turn seam — repl's `_home_input` uses
        # this so the NEW slot's turn is started through the SAME path a
        # normal Enter-idle submit uses (`slot.turn["task"]` actually gets
        # populated), instead of a bare `await` that ran the turn invisibly
        # (no busy glyph, no Esc/Ctrl-C cancel -- nothing ever recorded it).
        ui_hooks["start_turn_in"] = _start_turn_in
        # Attach-on-poll: the poller's own start-turn seam -- see
        # `_start_attach_in`'s docstring above. Same "no dock -> no entry"
        # fallback contract as start_turn_in: repl's attach_turn wiring
        # awaits the coroutine directly when no dock is present.
        ui_hooks["start_attach_in"] = _start_attach_in

    def _tab_fragments_live():
        # Live like _toolbar/_queue_fragments: re-invoked every redraw, so a
        # status_glyph flip (consent armed in a background tab) or an
        # active-slot change repaints the bar at once. forward=pane.
        # forward_mouse(clamp="top") (FIX6): first refusal on every tab-bar
        # click so a drag armed in the pane just below can still be
        # extended/completed once it releases up here, mirroring the
        # queue/todo panels' own forward=pane.forward_mouse below the pane.
        cols, _rows = sizing.get_size(get_app_or_none())
        return tab_fragments(slots, on_switch=_switch_to, on_close=_close_tab_click,
                             on_new=_new_tab_click, width=cols,
                             forward=lambda ev: _pane().forward_mouse(ev, clamp="top"))

    # The tab bar — pinned at the very TOP, fixed height 1, NEVER hidden
    # (unlike the queue/todo panels below it): it IS the new look, even with
    # only Home showing. focusable=False keeps focus on the input.
    # 0.3.25 (Valentin, live screenshot review): `style="class:tabbar"` seats
    # every chip on its own solid bar (`"tabbar": "bg:#262626"` in the Style
    # dict below) — a browser look, visually separated from the transcript
    # above/below it instead of floating directly on the terminal's own bg.
    tab_bar = Window(FormattedTextControl(_tab_fragments_live, focusable=False),
                     height=1, always_hide_cursor=True, style="class:tabbar")
    # ONE blank row of breathing room between the bar and the transcript —
    # deliberately bare (no style at all): it renders as plain terminal
    # background, transparent-looking, never a second colored stripe.
    tab_bar_spacer = Window(height=1)
    # The single most structural change of the W4a wave (map §3): the pane
    # slot in the root layout is a DynamicContainer, not a bound window —
    # it re-resolves `slots.active().pane.window` on EVERY redraw, so a tab
    # switch repaints a different slot's transcript with no stale reference
    # left over anywhere in the tree.
    pane_container = DynamicContainer(lambda: _pane().window)
    root = HSplit([tab_bar, tab_bar_spacer, pane_container, todo_panel, queue_panel,
                   Frame(input_win), toolbar])
    style = Style.from_dict(_STYLE_DICT)
    app = Application(layout=Layout(root, focused_element=input_win), key_bindings=kb,
                      full_screen=True, mouse_support=True, style=style)
    # Task 5: registering ("escape", "<digit>") chords makes bare Escape a
    # prefix of a longer match, so prompt_toolkit's key-processor now waits
    # up to `timeoutlen` (default 1.0s) before resolving a genuinely LONE
    # Escape (stop-turn / step-clear) when nothing follows it — a real,
    # noticeable regression for a key pressed to stop a turn RIGHT NOW. A
    # true Alt+digit press sends both bytes together (same write, same
    # packet even over SSH), so a much shorter window still disambiguates it
    # correctly; this only shortens the WAIT for a lone Escape, it changes
    # nothing about which binding ultimately fires.
    app.timeoutlen = 0.2
    configure_mouse_modes(app.output)   # ?1002 button-event, never ?1003 any-event
    # Part D: ANY keypress disarms every tab's busy-close confirm, same
    # contract as a tab switch above -- prompt_toolkit's own KeyProcessor
    # fires `after_key_press` for every key it resolves, key binding or
    # plain buffer insert alike, so this is the one universal hook that
    # needs no per-binding wiring at all.
    app.key_processor.after_key_press += lambda _e: disarm_all(slots)

    async def _ticker():
        # animate the spinner + tick the elapsed clock while a turn runs.
        # _tick_once runs UNCONDITIONALLY every tick, busy or idle — a
        # resize while idle must re-wrap the transcript too, and the
        # no-change cost is just two int reads. It resolves the ACTIVE
        # slot's pane itself (slots, not a bound pane) -- _busy_live is the
        # is_busy this ticker feeds it, since the old top-level is_busy
        # param died with the rest of the sink-shaped params.
        while True:
            await asyncio.sleep(0.25)
            _sync_hover_mode()
            _tick_once(slots, app, _busy_live)

    # FIX7c (W4a final review — history seeding): the FIRST active slot's
    # own history is pointed at from the START, before a single key is
    # pressed -- not only on the FIRST actual `_switch_to` call. Without
    # this, every line typed pre-switch recorded into the Buffer's own
    # THROWAWAY default `InMemoryHistory()` (never touched `slot.history`,
    # which stayed None); the first later switch away and back then MINTED
    # a brand-new empty history for the slot (`_swap_history`'s own None
    # check), silently losing every line typed before that first switch --
    # ↑ recall on a slot you never left would work, but come back after a
    # Home-and-back and it's gone.
    _swap_history(buf, _a())

    tick = asyncio.ensure_future(_ticker())
    try:
        await app.run_async()
    finally:
        tick.cancel()
    return True
