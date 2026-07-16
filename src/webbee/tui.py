"""Full-screen dock: a scrollable, colored output pane (Rich → ANSI, see
output_pane.py) fills the top; a bordered input box + toolbar are pinned at
the very bottom and never move while the output scrolls (mouse wheel /
PageUp). `run_session` also drives step-navigation (Up/Down + Enter) over the
pinned box when the input is empty and no turn is running; in every other
state Up/Down recall submitted lines (readline-style), and Enter while a turn
runs QUEUES the line (Claude-Code type-ahead: it runs after the current
turn). Pure helpers (next_mode/build_toolbar/the *_action functions) are
unit-tested; the Application is TTY/headless-smoke verified. Grounded in
prompt_toolkit 3.0.52."""
import asyncio
import re
from collections import deque

from webbee.output_pane import OutputPane  # noqa: F401 — re-exported (webbee.tui.OutputPane)
from webbee.render import _fmt_tokens

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
                  consent: bool = False, queued: int = 0) -> list:
    """The status line under the pinned input box, as prompt_toolkit formatted
    text (per-segment styled). Three states: consent (awaiting a reply), busy
    (a turn is running — an ANIMATED coloured spinner + the current action in
    accent, so it pops, not grey), and idle (mode value coloured PER MODE —
    default cyan / plan purple / autopilot yellow — + SESSION spend + the
    Shift + TAB hint). `queued` = type-ahead lines waiting to run after the
    current turn; when >0 a subtle `⋯N queued` segment shows in the busy AND
    idle states. Style classes are defined in run_session's Style."""
    q = f" · ⋯{queued} queued" if queued else ""
    if consent:
        return [("class:tb.consent", "  approve? type y / n / a reply · Enter to send")]
    if busy:
        spin = _SPINNER[int(elapsed * 10) % len(_SPINNER)]   # animates via the ticker
        frags = [("class:tb.spin", f"  {spin} "), ("class:tb.working", "working")]
        if current:
            frags += [("class:tb.dim", " · "), ("class:tb.action", current)]
        frags.append(("class:tb.dim",
                      f" · {elapsed:.0f}s · {tools} · {_fmt_tokens(tokens)} tok{q}"
                      f"   ·   Esc/Ctrl-C to stop"))
        return frags
    return [("class:tb.dim", "  mode: "),
            (f"class:tb.mode.{mode}", mode),
            ("class:tb.dim",
             f"   ·   {_fmt_tokens(tokens)} tok · {_fmt_tokens(credits)} credits{q}   ·   Shift + TAB: switch mode")]


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
        t.cancel()                          # cancel the running turn; dock survives


def _submit_line(text: str, buf, pending, busy: bool, start) -> str:
    """Route ONE non-empty submitted line (dependency-injected, same testing
    philosophy as _escape_action). Records it into the buffer's history first
    (up-arrow recall), then: busy → QUEUE it (Claude-Code type-ahead — the
    line is never erased or dropped; it runs after the current turn); idle →
    `start(text)` (today's normal submit). Returns "queued" | "started"."""
    buf.history.append_string(text)
    if busy:
        pending.append(text)
        return "queued"
    start(text)
    return "started"


def _drain_pending(pending, start) -> bool:
    """Turn-completion drain: pop the OLDEST queued type-ahead line and hand
    it to `start` — the SAME path a typed line takes, so a persistent
    marathon receives it as a `new_task` into the running session. ONE item
    per completion; the rest stay queued FIFO (each finished turn drains the
    next). Returns True when a drain happened."""
    if not pending:
        return False
    start(pending.popleft())
    return True


def _arrow_up_action(event, buf, sel: dict, n: int, busy: bool) -> None:
    """Up key. Step-navigation EXACTLY as before when steps exist + input is
    empty + idle; in EVERY other state (busy, or text present, or no steps)
    recall an older submitted line into the buffer (readline-style)."""
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


async def run_session(*, pane, on_line, mode_getter, on_cycle, status,
                      is_busy, consent_pending, resolve_consent, steps_nav=None,
                      stop_turn=None) -> bool:
    """The full-screen dock: `pane` fills the top (scrollable), a bordered input
    box + toolbar are FIXED at the bottom. Enter either resolves a pending
    consent reply (ICNLI: raw verbatim) or starts a turn as a BACKGROUND task
    (the box stays fixed during it); while a turn runs, Enter QUEUES the line
    (type-ahead — it starts right after the current turn; the toolbar shows
    `⋯N queued`). When `steps_nav` is given and the input is empty and no turn
    is running, Up/Down move a step selection (toolbar shows `step k/N`) and
    Enter expands it via `steps_nav["expand"]`; Esc clears it. In every other
    state Up/Down recall submitted lines (readline-style history).
    Returns True on clean exit; False if prompt_toolkit is unavailable (caller
    uses the plain fallback loop)."""
    try:
        from prompt_toolkit.application import Application, get_app
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except Exception:
        return False

    buf = Buffer(multiline=False)
    turn = {"task": None}
    pending: deque = deque()   # type-ahead queue: lines submitted while a turn runs

    def _start_turn(text):
        turn["task"] = get_app().create_background_task(_run_turn(text))

    async def _run_turn(text):
        done = False
        try:
            await on_line(text)
            done = True
        finally:
            turn["task"] = None
            if done:
                # Type-ahead drain: the turn finished normally (an Esc/Ctrl-C
                # stop counts — repl._run_turn absorbs the cancel) → submit the
                # oldest queued line through the SAME path a typed line takes.
                # A propagating exception (dock teardown) leaves the queue be.
                _drain_pending(pending, _start_turn)
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
        text = scrub_mouse_residue(buf.text)   # never send leaked mouse reports
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
        # Non-empty line: record for up-arrow recall, then queue-while-busy
        # (type-ahead — runs after the current turn) or start a turn now.
        if _submit_line(text, buf, pending, _busy_live(), _start_turn) == "queued":
            event.app.invalidate()             # toolbar shows the new depth

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
        _arrow_up_action(event, buf, sel, _nav_count(), _busy_live())

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
                             queued=len(pending))

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

    input_win = Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput(_prompt_fragments)]),
        height=_input_height, wrap_lines=True)
    toolbar = Window(FormattedTextControl(_toolbar), height=1, always_hide_cursor=True)
    root = HSplit([pane.window, Frame(input_win), toolbar])
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
