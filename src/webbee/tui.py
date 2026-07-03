"""prompt_toolkit-backed input: the typed message in a block between a top and
bottom full-width bar, a toolbar (mode · session tokens · $) under it, and
Shift+Tab to cycle the mode. Pure helpers (next_mode/build_toolbar) are unit-
tested; the interactive prompt() is thin and TTY-verified, with an input()
fallback so it never hard-fails."""
import asyncio

from webbee.render import _fmt_tokens

_MODES = ("default", "plan", "autopilot")


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
    live status the old Rich spinner used to show), and idle (mode + spend +
    the Shift + TAB hint spelled in words)."""
    if consent:
        return "  approve? type y / n / a reply · Enter to send"
    if busy:
        _cur = f" · {current}" if current else ""
        return (f"  ● working{_cur} · {elapsed:.0f}s · {tools}"
                f" · {_fmt_tokens(tokens)} tok   ·   Ctrl-C to stop")
    return (f"  mode: {mode}   ·   {_fmt_tokens(tokens)} tok · ${cost:.4f}"
            f"   ·   Shift + TAB: switch mode")


def _build_app(buf, kb, toolbar_getter):
    """Build the non-fullscreen Application: a bordered input box docked at the
    bottom with a toolbar under it. full_screen=False keeps native scrollback
    (output printed between prompts stays above the box)."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.processors import BeforeInput
    from prompt_toolkit.widgets import Frame

    body = Frame(Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput("❯ ")]),
        height=1, wrap_lines=True))
    toolbar = Window(FormattedTextControl(toolbar_getter), height=1, always_hide_cursor=True)
    root = HSplit([body, toolbar])
    return Application(layout=Layout(root, focused_element=body),
                       key_bindings=kb, full_screen=False, mouse_support=False)


async def prompt(*, mode_getter, usage_getter, on_cycle) -> "str | None":
    """Show the bordered docked input box + toolbar; return the typed line
    (None on EOF/interrupt). The box is docked at the bottom and re-rendered
    each input, so turn output printed between prompts stays above it and the
    terminal's native scrollback is preserved (full_screen=False). Falls back
    to builtin input() if prompt_toolkit can't run (no tty / error)."""
    try:
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.key_binding import KeyBindings
    except Exception:
        return _fallback_input()

    result = {"text": None}
    buf = Buffer(multiline=False)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        result["text"] = buf.text
        event.app.exit()

    @kb.add("s-tab")
    def _cycle(event):
        on_cycle()
        event.app.invalidate()   # redraw the toolbar with the new mode

    @kb.add("c-c")
    @kb.add("c-d")
    def _cancel(event):
        result["text"] = None
        event.app.exit()

    def _toolbar():
        tok, cost = usage_getter()
        return build_toolbar(mode_getter(), tok, cost)

    try:
        app = _build_app(buf, kb, _toolbar)
        await app.run_async()
        return result["text"]
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        return _fallback_input()


async def run_session(*, on_line, mode_getter, on_cycle, status,
                      is_busy, consent_pending, resolve_consent) -> bool:
    """The persistent dock: a bordered input box + toolbar pinned at the bottom
    for the WHOLE session (non-fullscreen → native scrollback kept). A submitted
    line either resolves a pending consent reply (ICNLI: raw verbatim) or starts
    a turn as a BACKGROUND task, so the box stays pinned during the turn. Turn
    output is printed by the sink and lands above the box (repl wraps this in
    patch_stdout). Returns True after a clean exit; False if prompt_toolkit is
    unavailable so the caller can use the plain fallback loop."""
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
            t.cancel()                          # cancel the running turn; box survives

    @kb.add("c-d")
    def _eof(event):
        if not is_busy():
            event.app.exit()

    def _toolbar():
        st = status()
        return build_toolbar(mode_getter(), st["tokens"], st["cost"], busy=st["busy"],
                             current=st["current"], elapsed=st["elapsed"],
                             tools=st["tools"], consent=st["consent"])

    body = Frame(Window(
        BufferControl(buffer=buf, input_processors=[BeforeInput("❯ ", style="class:prompt")]),
        height=1, wrap_lines=True))
    toolbar = Window(FormattedTextControl(_toolbar), height=1,
                     always_hide_cursor=True, style="class:toolbar")
    root = HSplit([body, toolbar])
    style = Style.from_dict({
        "frame.border": "#5f5f5f",       # muted grey chrome — furniture, not focus
        "toolbar": "#8a8a8a",            # dim
        "prompt": "#00afd7 bold",        # cyan ❯ — the one interactive accent
    })
    app = Application(layout=Layout(root, focused_element=body), key_bindings=kb,
                      full_screen=False, mouse_support=False, style=style)

    async def _ticker():
        # tick the elapsed clock while a turn runs (usage/tool frames also
        # invalidate, so counters feel live without a fast tick)
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


def _fallback_input() -> "str | None":
    try:
        return input("❯ ")
    except (EOFError, KeyboardInterrupt):
        return None
