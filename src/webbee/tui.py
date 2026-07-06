"""Full-screen dock: a scrollable, colored output pane (Rich → ANSI, see
output_pane.py) fills the top; a bordered input box + toolbar are pinned at
the very bottom and never move while the output scrolls (mouse wheel /
PageUp). `run_session` also drives step-navigation (Up/Down + Enter) over the
pinned box when the input is empty and no turn is running. Pure helpers
(next_mode/build_toolbar) are unit-tested; the Application is TTY/headless-
smoke verified. Grounded in prompt_toolkit 3.0.52."""
import asyncio

from webbee.output_pane import OutputPane  # noqa: F401 — re-exported (webbee.tui.OutputPane)
from webbee.render import _fmt_tokens

_MODES = ("default", "plan", "autopilot")
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille frames — animated while a turn runs


def next_mode(mode: str) -> str:
    try:
        return _MODES[(_MODES.index(mode) + 1) % len(_MODES)]
    except ValueError:
        return _MODES[0]


def build_toolbar(mode: str, tokens: int, credits: int, *, busy: bool = False,
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
             f"   ·   {_fmt_tokens(tokens)} tok · {credits} credits   ·   Shift + TAB: switch mode")]


async def run_session(*, pane, on_line, mode_getter, on_cycle, status,
                      is_busy, consent_pending, resolve_consent, steps_nav=None) -> bool:
    """The full-screen dock: `pane` fills the top (scrollable), a bordered input
    box + toolbar are FIXED at the bottom. Enter either resolves a pending
    consent reply (ICNLI: raw verbatim) or starts a turn as a BACKGROUND task
    (the box stays fixed during it). When `steps_nav` is given and the input is
    empty and no turn is running, Up/Down move a step selection (toolbar shows
    `step k/N`) and Enter expands it via `steps_nav["expand"]`; Esc clears it.
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
        if not text.strip() and sel["i"] is not None and steps_nav and not is_busy():
            idx, sel["i"] = sel["i"], None
            event.app.create_background_task(steps_nav["expand"](idx))
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

    sel = {"i": None}   # None = no selection; else 0-based step index

    def _nav_count() -> int:
        try:
            return int(steps_nav["count"]()) if steps_nav else 0
        except Exception:
            return 0

    @kb.add("up")
    def _step_up(event):
        n = _nav_count()
        if n and not buf.text and not is_busy():
            sel["i"] = (n - 1) if sel["i"] is None else max(0, sel["i"] - 1)
            event.app.invalidate()

    @kb.add("down")
    def _step_down(event):
        n = _nav_count()
        if n and not buf.text and not is_busy():
            sel["i"] = 0 if sel["i"] is None else min(n - 1, sel["i"] + 1)
            event.app.invalidate()

    @kb.add("escape")
    def _step_clear(event):
        sel["i"] = None
        event.app.invalidate()

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
