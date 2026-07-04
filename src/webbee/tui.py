"""Full-screen dock: a scrollable, colored output pane (Rich → ANSI) fills the
top; a bordered input box + toolbar are pinned at the very bottom and never
move while the output scrolls (mouse wheel / PageUp). Pure helpers
(next_mode/build_toolbar) are unit-tested; the Application + OutputPane are
TTY/headless-smoke verified. Grounded in prompt_toolkit 3.0.52."""
import asyncio

from webbee.render import _fmt_tokens

_MODES = ("default", "plan", "autopilot")
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille frames — animated while a turn runs


def next_mode(mode: str) -> str:
    try:
        return _MODES[(_MODES.index(mode) + 1) % len(_MODES)]
    except ValueError:
        return _MODES[0]


def build_toolbar(mode: str, tokens: int, cost: float, *, busy: bool = False,
                  current: str = "", elapsed: float = 0.0, tools: int = 0,
                  consent: bool = False) -> list:
    """The status line under the pinned input box, as prompt_toolkit formatted
    text (per-segment styled). Three states: consent (awaiting a reply), busy
    (a turn is running — an ANIMATED coloured spinner + the current action in
    accent, so it pops, not grey), and idle (mode value coloured PER MODE —
    default cyan / plan purple / autopilot yellow — + SESSION spend + the
    Shift + TAB hint). Style classes are defined in run_session's Style."""
    if consent:
        return [("class:tb.consent", "  approve? type y / n / a reply · Enter to send")]
    if busy:
        spin = _SPINNER[int(elapsed * 10) % len(_SPINNER)]   # animates via the ticker
        frags = [("class:tb.spin", f"  {spin} "), ("class:tb.working", "working")]
        if current:
            frags += [("class:tb.dim", " · "), ("class:tb.action", current)]
        frags.append(("class:tb.dim",
                      f" · {elapsed:.0f}s · {tools} · {_fmt_tokens(tokens)} tok"
                      f"   ·   Ctrl-C to stop"))
        return frags
    return [("class:tb.dim", "  mode: "),
            (f"class:tb.mode.{mode}", mode),
            ("class:tb.dim",
             f"   ·   {_fmt_tokens(tokens)} tok · ${cost:.4f}   ·   Shift + TAB: switch mode")]


class OutputPane:
    """A full-screen scrollable pane that shows COLORED output. Rich renders
    into a StringIO as ANSI; a FormattedTextControl(ANSI(...)) shows it with the
    colours intact. The `get_cursor_position == vertical_scroll` trick makes
    Window.vertical_scroll authoritative so the mouse wheel / PageUp scroll it;
    notify() auto-follows the tail ONLY when the user is already at the bottom
    (so scrolling up to read history isn't yanked back down). Grounded in
    prompt_toolkit 3.0.52 (verified in venv)."""

    def __init__(self, width: int = 100) -> None:
        import io

        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType, MouseButton
        from rich.console import Console

        self._io = io.StringIO()
        self.console = Console(file=self._io, force_terminal=True,
                               color_system="truecolor", width=width, highlight=False)
        self._ANSI = ANSI
        self._cache = (None, None)   # (text, ANSI) memo — bounds re-parse cost
        self.copy_flash = ""         # transient toolbar note after a copy
        self._flash_until = 0.0
        pane = self

        # Copy-on-select: left-drag over the pane copies the covered text to the
        # system clipboard via OSC 52 (works locally + over SSH). The Window
        # gives us CONTENT coordinates (Point(x=col, y=row)); with wrap_lines
        # False, row = content line, col = char index. SCROLL events return
        # NotImplemented so the Window keeps handling the wheel.
        class _SelectControl(FormattedTextControl):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._down = None

            def mouse_handler(self, ev):
                et = ev.event_type
                if et == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.LEFT:
                    self._down = ev.position
                    return None
                if et == MouseEventType.MOUSE_MOVE:
                    return None if self._down is not None else NotImplemented
                if et == MouseEventType.MOUSE_UP:
                    down, self._down = self._down, None
                    if down is not None and (down.x, down.y) != (ev.position.x, ev.position.y):
                        pane._copy_selection(down, ev.position)  # real drag, not a click
                    return None
                return NotImplemented   # SCROLL_UP/DOWN → Window scrolls

        self.control = _SelectControl(
            text=self._formatted, focusable=False, show_cursor=False,
            get_cursor_position=lambda: Point(0, self.window.vertical_scroll))
        self.window = Window(content=self.control, wrap_lines=False,
                             always_hide_cursor=True, allow_scroll_beyond_bottom=False)

    def _formatted(self):
        text = self._io.getvalue()
        if self._cache[0] != text:                # only re-parse when it changed
            self._cache = (text, self._ANSI(text))
        return self._cache[1]

    def notify(self) -> None:
        """Called after each sink print: follow the tail if the user is at the
        bottom, then redraw. If they've scrolled up, leave their scroll alone."""
        self._trim()
        ri = self.window.render_info
        if ri is not None and ri.bottom_visible:
            n = self._io.getvalue().count("\n") + 1
            self.window.vertical_scroll = max(0, n - ri.window_height)
        try:
            from prompt_toolkit.application import get_app_or_none
            app = get_app_or_none()
            if app is not None:
                app.invalidate()
        except Exception:
            pass

    def _trim(self, max_lines: int = 5000) -> None:
        import io
        s = self._io.getvalue()
        if s.count("\n") > max_lines:
            s = "\n".join(s.split("\n")[-max_lines:])
            self._io = io.StringIO()
            self._io.write(s)
            self.console.file = self._io

    def dump(self) -> str:
        """The full session transcript (ANSI). Printed to real stdout on exit so
        the conversation survives leaving the alternate screen."""
        return self._io.getvalue()

    # ---- copy-on-select --------------------------------------------------
    def _selected_text(self, start, end) -> str:
        """Plain text (ANSI stripped) covered by a drag from `start` to `end`
        (both Points with content col=x / line=y). wrap_lines is False so a
        content line maps 1:1 to a `\\n`-split line and col = char index."""
        import re
        plain = re.sub(r"\x1b\[[0-9;]*m", "", self._io.getvalue())
        lines = plain.split("\n")
        p1, p2 = (start.y, start.x), (end.y, end.x)
        if p1 > p2:
            p1, p2 = p2, p1
        (y1, x1), (y2, x2) = p1, p2
        n = len(lines)
        if not (0 <= y1 < n):
            return ""
        y2 = min(y2, n - 1)
        if y1 == y2:
            return lines[y1][x1:x2 + 1]
        out = [lines[y1][x1:]]
        out.extend(lines[y1 + 1:y2])
        out.append(lines[y2][:x2 + 1])
        return "\n".join(out)

    def _osc52_copy(self, text: str) -> None:
        import base64
        try:
            from prompt_toolkit.application import get_app_or_none
            app = get_app_or_none()
            if app is None:
                return
            b64 = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
            app.output.write_raw("\x1b]52;c;" + b64 + "\x07")  # OSC 52 — set clipboard
            app.output.flush()
        except Exception:
            pass

    def _copy_selection(self, start, end) -> None:
        import time as _t
        text = self._selected_text(start, end)
        if not text.strip():
            return
        self._osc52_copy(text)
        self.copy_flash = f"✓ copied {len(text)} char{'s' if len(text) != 1 else ''}"
        self._flash_until = _t.monotonic() + 1.8
        self.notify()

    def flash(self) -> str:
        """The transient 'copied' note, while still fresh (else empty)."""
        import time as _t
        return self.copy_flash if _t.monotonic() < self._flash_until else ""


async def run_session(*, pane, on_line, mode_getter, on_cycle, status,
                      is_busy, consent_pending, resolve_consent) -> bool:
    """The full-screen dock: `pane` fills the top (scrollable), a bordered input
    box + toolbar are FIXED at the bottom. Enter either resolves a pending
    consent reply (ICNLI: raw verbatim) or starts a turn as a BACKGROUND task
    (the box stays fixed during it). Returns True on clean exit; False if
    prompt_toolkit is unavailable (caller uses the plain fallback loop)."""
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

    async def _run_turn(text):
        try:
            await on_line(text)
        finally:
            turn["task"] = None
            get_app().invalidate()

    kb = KeyBindings()

    @kb.add("enter")
    def _enter(event):
        text = buf.text
        buf.reset()
        if consent_pending():
            resolve_consent(text)              # ICNLI: relay the raw reply verbatim
            return
        if is_busy() or not text.strip():
            return
        turn["task"] = event.app.create_background_task(_run_turn(text))

    @kb.add("s-tab")
    def _cycle(event):
        on_cycle()
        event.app.invalidate()

    @kb.add("c-c")
    def _interrupt(event):
        t = turn["task"]
        if t is not None and not t.done():
            t.cancel()                          # cancel the running turn; dock survives

    @kb.add("c-d")
    def _eof(event):
        if not is_busy():
            event.app.exit()

    @kb.add("pageup")
    def _pgup(event):
        pane.window.vertical_scroll = max(0, pane.window.vertical_scroll - 5)

    @kb.add("pagedown")
    def _pgdn(event):
        pane.window.vertical_scroll += 5

    def _toolbar():
        f = pane.flash()
        if f:
            return [("class:tb.working", "  " + f)]   # transient copy confirmation
        st = status()
        return build_toolbar(mode_getter(), st["tokens"], st["cost"], busy=st["busy"],
                             current=st["current"], elapsed=st["elapsed"],
                             tools=st["tools"], consent=st["consent"])

    input_win = Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput("❯ ", style="class:prompt")]),
        height=1)
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
