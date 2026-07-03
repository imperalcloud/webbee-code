"""prompt_toolkit-backed input: the typed message in a block between a top and
bottom full-width bar, a toolbar (mode · session tokens · $) under it, and
Shift+Tab to cycle the mode. Pure helpers (next_mode/build_toolbar) are unit-
tested; the interactive prompt() is thin and TTY-verified, with an input()
fallback so it never hard-fails."""
import shutil

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


async def prompt(*, mode_getter, usage_getter, on_cycle) -> "str | None":
    """Show the boxed input + toolbar and return the typed line (None on EOF).
    Falls back to builtin input() if prompt_toolkit can't run (no tty / error)."""
    try:
        from prompt_toolkit import PromptSession, print_formatted_text
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
    except Exception:
        return _fallback_input()

    kb = KeyBindings()

    @kb.add("s-tab")
    def _cycle(event):
        on_cycle()
        event.app.invalidate()   # redraw the toolbar with the new mode

    def bottom_toolbar():
        cols = shutil.get_terminal_size((80, 24)).columns
        tok, cost = usage_getter()
        return FormattedText([
            ("class:rule", "─" * cols),
            ("", "\n"),
            ("class:tb", build_toolbar(mode_getter(), tok, cost)),
        ])

    try:
        cols = shutil.get_terminal_size((80, 24)).columns
        print_formatted_text(FormattedText([("class:rule", "─" * cols)]))
        session = PromptSession()
        return await session.prompt_async(
            "❯ ", bottom_toolbar=bottom_toolbar, key_bindings=kb)
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        return _fallback_input()


def _fallback_input() -> "str | None":
    try:
        return input("❯ ")
    except (EOFError, KeyboardInterrupt):
        return None
