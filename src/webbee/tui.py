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
    """A scrollable, colored output pane. Rich renders the full transcript into
    a StringIO as ANSI; the pane VIRTUALIZES — the FormattedTextControl is fed
    ONLY the currently-visible slice of lines, so every frame costs O(viewport),
    not O(session). That keeps huge sessions lag-free and never truncates a long
    answer (the visible region is always rendered in full; scroll to see more).
    Wheel / PageUp move `_offset`; left-drag copies the covered text via OSC 52.
    Grounded in prompt_toolkit 3.0.52 (verified in venv)."""

    def __init__(self, width: int = 100) -> None:
        import io

        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType, MouseButton
        from rich.console import Console

        self._io = io.StringIO()
        self.console = Console(file=self._io, force_terminal=True,
                               color_system="truecolor", width=width, highlight=False)
        self._ANSI = ANSI
        self._lines_cache = (None, [""])   # (text, split-lines) — memoize the split
        self._offset = 0                   # index of the top visible line
        self._view_h = 20                  # viewport height (updated from render_info)
        self._follow = True                # stick to the tail unless the user scrolls up
        self._sel = None                   # (abs_start, abs_end) during a drag → live highlight
        self._plain_cache = (None, [""])   # (text, ANSI-stripped lines) for select/highlight
        self.copy_flash = ""               # transient toolbar note after a copy
        self._flash_until = 0.0
        pane = self

        # Content fed to the control is only the visible slice, so the mouse row
        # is a VIEWPORT row (0..view_h-1) — add `_offset` for the absolute line.
        # Wheel scroll adjusts `_offset` directly (the control shows no more than
        # a viewport, so there is nothing for the Window itself to scroll).
        class _SelectControl(FormattedTextControl):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._down = None

            def mouse_handler(self, ev):
                et = ev.event_type
                if et == MouseEventType.SCROLL_UP:
                    pane.scroll(-3)
                    return None
                if et == MouseEventType.SCROLL_DOWN:
                    pane.scroll(3)
                    return None
                if et == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.LEFT:
                    self._down = ev.position
                    a = (ev.position.y + pane._offset, ev.position.x)
                    pane._sel = (a, a)                 # zero-width start (no highlight yet)
                    pane._invalidate()
                    return None
                if et == MouseEventType.MOUSE_MOVE:
                    if self._down is None:
                        return NotImplemented
                    pane._sel = ((self._down.y + pane._offset, self._down.x),
                                 (ev.position.y + pane._offset, ev.position.x))
                    pane._invalidate()                 # grow the highlight as you drag
                    return None
                if et == MouseEventType.MOUSE_UP:
                    down, self._down = self._down, None
                    if down is not None and (down.x, down.y) != (ev.position.x, ev.position.y):
                        pane._copy_selection(down, ev.position)  # real drag, not a click
                    pane._sel = None
                    pane._invalidate()                 # clear the highlight (colours restored)
                    return None
                return NotImplemented

        self.control = _SelectControl(text=self._formatted, focusable=False, show_cursor=False)
        self.window = Window(content=self.control, wrap_lines=False, always_hide_cursor=True)

    # ---- virtualized render ---------------------------------------------
    def _all_lines(self):
        s = self._io.getvalue()
        if self._lines_cache[0] != s:            # re-split only when the buffer changed
            self._lines_cache = (s, s.split("\n"))
        return self._lines_cache[1]

    def _plain_lines(self):
        import re
        s = self._io.getvalue()
        if self._plain_cache[0] != s:
            self._plain_cache = (s, re.sub(r"\x1b\[[0-9;]*m", "", s).split("\n"))
        return self._plain_cache[1]

    def _norm_sel(self):
        (a, b) = self._sel
        return (a, b) if a <= b else (b, a)

    def _formatted(self):
        ri = self.window.render_info
        if ri is not None and ri.window_height:
            self._view_h = ri.window_height
        lines = self._all_lines()
        h = max(1, self._view_h)
        off = max(0, min(self._offset, max(0, len(lines) - h)))
        self._offset = off
        visible = lines[off:off + h]
        if self._sel is None:
            return self._ANSI("\n".join(visible))
        # A drag is in progress — overlay reverse-video on the selected columns
        # (selected lines render plain+reversed; colours return when it clears).
        (y1, x1), (y2, x2) = self._norm_sel()
        plain = self._plain_lines()
        out = []
        for vi, aln in enumerate(range(off, off + len(visible))):
            if y1 <= aln <= y2 and aln < len(plain):
                ln = plain[aln]
                a = max(0, min(x1 if aln == y1 else 0, len(ln)))
                b = max(-1, min(x2 if aln == y2 else len(ln) - 1, len(ln) - 1))
                out.append(ln[:a] + "\x1b[7m" + ln[a:b + 1] + "\x1b[0m" + ln[b + 1:]
                           if b >= a else ln)
            else:
                out.append(visible[vi])
        return self._ANSI("\n".join(out))

    def scroll(self, delta: int) -> None:
        lines = self._all_lines()
        max_off = max(0, len(lines) - max(1, self._view_h))
        self._offset = max(0, min(self._offset + delta, max_off))
        self._follow = self._offset >= max_off   # re-arm tail-follow once back at bottom
        self._invalidate()

    def notify(self) -> None:
        """After each sink print: follow the tail unless the user scrolled up."""
        self._trim()
        lines = self._all_lines()
        max_off = max(0, len(lines) - max(1, self._view_h))
        self._offset = max_off if self._follow else min(self._offset, max_off)
        self._invalidate()

    def _invalidate(self) -> None:
        try:
            from prompt_toolkit.application import get_app_or_none
            app = get_app_or_none()
            if app is not None:
                app.invalidate()
        except Exception:
            pass

    def _trim(self, max_lines: int = 20000) -> None:
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
        """Plain text (ANSI stripped) covered by a drag. start/end .y are
        VIEWPORT rows → add `_offset` for absolute content lines. wrap_lines is
        False so a content line maps 1:1 to a `\\n`-split line, col = char index."""
        lines = self._plain_lines()
        p1 = (start.y + self._offset, start.x)
        p2 = (end.y + self._offset, end.x)
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
        pane.scroll(-(max(1, pane._view_h) - 2))

    @kb.add("pagedown")
    def _pgdn(event):
        pane.scroll(max(1, pane._view_h) - 2)

    def _toolbar():
        f = pane.flash()
        if f:
            return [("class:tb.working", "  " + f)]   # transient copy confirmation
        st = status()
        return build_toolbar(mode_getter(), st["tokens"], st["cost"], busy=st["busy"],
                             current=st["current"], elapsed=st["elapsed"],
                             tools=st["tools"], consent=st["consent"])

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

    input_win = Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput("❯ ", style="class:prompt")]),
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
