import re

from webbee.tui import next_mode, build_toolbar

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def test_next_mode_cycles():
    assert next_mode("default") == "plan"
    assert next_mode("plan") == "autopilot"
    assert next_mode("autopilot") == "default"

def test_next_mode_unknown_resets():
    assert next_mode("weird") == "default"

def test_toolbar_has_mode_tokens_cost_and_hint():
    t = build_toolbar("plan", 51000, 0.0664)
    assert "plan" in t
    assert "51.0k" in t
    assert "$0.0664" in t
    assert "Shift + TAB" in t          # spelled in words, no glyph
    assert "⇧⇥" not in t     # the ⇧⇥ glyph must NOT appear
    assert not NO_CYRILLIC.search(t)


def test_fallback_input_used_off_tty(monkeypatch):
    # When prompt_toolkit can't be imported, prompt() must degrade to _fallback_input.
    import webbee.tui as tui
    monkeypatch.setattr(tui, "_fallback_input", lambda: "typed line")
    import builtins, asyncio
    real_import = builtins.__import__
    def boom(name, *a, **k):
        if name.startswith("prompt_toolkit"):
            raise ImportError("no ptk")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", boom)
    out = asyncio.run(tui.prompt(mode_getter=lambda: "default",
                                 usage_getter=lambda: (0, 0.0), on_cycle=lambda: None))
    assert out == "typed line"


def test_build_app_constructs_and_is_not_fullscreen():
    # The layout builds without a tty and stays non-fullscreen (scrollback kept).
    import webbee.tui as tui
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings
    buf = Buffer(multiline=False)
    kb = KeyBindings()
    app = tui._build_app(buf, kb, lambda: build_toolbar("default", 0, 0.0))
    assert app.full_screen is False          # never alt-screen (scrollback kept)
