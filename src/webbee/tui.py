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
                  consent: bool = False) -> str:
    """The single status line under the pinned input box. Three states:
    consent (awaiting an approve/deny reply), busy (a turn is running — the
    live status the old Rich spinner used to show, with an animated frame), and
    idle (mode + SESSION spend + the Shift + TAB hint spelled in words)."""
    if consent:
        return "  approve? type y / n / a reply · Enter to send"
    if busy:
        _cur = f" · {current}" if current else ""
        _spin = _SPINNER[int(elapsed * 10) % len(_SPINNER)]   # animates via the ticker
        return (f"  {_spin} working{_cur} · {elapsed:.0f}s · {tools}"
                f" · {_fmt_tokens(tokens)} tok   ·   Ctrl-C to stop")
    return (f"  mode: {mode}   ·   {_fmt_tokens(tokens)} tok · ${cost:.4f}"
            f"   ·   Shift + TAB: switch mode")


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
        from rich.console import Console

        self._io = io.StringIO()
        self.console = Console(file=self._io, force_terminal=True,
                               color_system="truecolor", width=width, highlight=False)
        self._ANSI = ANSI
        self._cache = (None, None)   # (text, ANSI) memo — bounds re-parse cost
        self.control = FormattedTextControl(
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
        st = status()
        return build_toolbar(mode_getter(), st["tokens"], st["cost"], busy=st["busy"],
                             current=st["current"], elapsed=st["elapsed"],
                             tools=st["tools"], consent=st["consent"])

    input_win = Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput("❯ ", style="class:prompt")]),
        height=1)
    toolbar = Window(FormattedTextControl(_toolbar), height=1,
                     always_hide_cursor=True, style="class:toolbar")
    root = HSplit([pane.window, Frame(input_win), toolbar])
    style = Style.from_dict({
        "frame.border": "#5f5f5f",       # muted grey chrome — furniture, not focus
        "toolbar": "#8a8a8a",            # dim
        "prompt": "#00afd7 bold",        # cyan ❯ — the one interactive accent
    })
    app = Application(layout=Layout(root, focused_element=input_win), key_bindings=kb,
                      full_screen=True, mouse_support=True, style=style)

    async def _ticker():
        # animate the spinner + tick the elapsed clock while a turn runs
        while True:
            await asyncio.sleep(0.25)
            if is_busy():
                app.invalidate()

    tick = asyncio.ensure_future(_ticker())
    try:
        await app.run_async()
    finally:
        tick.cancel()
    return True
