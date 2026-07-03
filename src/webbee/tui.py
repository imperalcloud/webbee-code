"""prompt_toolkit-backed input: the typed message in a block between a top and
bottom full-width bar, a toolbar (mode · session tokens · $) under it, and
Shift+Tab to cycle the mode. Pure helpers (next_mode/build_toolbar) are unit-
tested; the interactive prompt() is thin and TTY-verified, with an input()
fallback so it never hard-fails."""
from webbee.render import _fmt_tokens

_MODES = ("default", "plan", "autopilot")


def next_mode(mode: str) -> str:
    try:
        return _MODES[(_MODES.index(mode) + 1) % len(_MODES)]
    except ValueError:
        return _MODES[0]


def build_toolbar(mode: str, tokens: int, cost: float) -> str:
    return (f" mode: {mode}   ·   🔤 {_fmt_tokens(tokens)} · ${cost:.4f}"
            f"   ·   Shift + TAB: switch mode ")


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


def _fallback_input() -> "str | None":
    try:
        return input("❯ ")
    except (EOFError, KeyboardInterrupt):
        return None
