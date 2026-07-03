import re

from webbee.tui import next_mode, build_toolbar

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def _txt(frags):
    """Join prompt_toolkit formatted-text fragments into the visible string."""
    return "".join(seg for _, seg in frags)


def test_next_mode_cycles():
    assert next_mode("default") == "plan"
    assert next_mode("plan") == "autopilot"
    assert next_mode("autopilot") == "default"

def test_next_mode_unknown_resets():
    assert next_mode("weird") == "default"

def test_toolbar_idle_has_mode_tokens_cost_and_hint():
    t = _txt(build_toolbar("plan", 51000, 0.0664))
    assert "plan" in t
    assert "51.0k" in t
    assert "$0.0664" in t
    assert "Shift + TAB" in t          # spelled in words, no glyph
    assert "⇧⇥" not in t     # the ⇧⇥ glyph must NOT appear
    assert not NO_CYRILLIC.search(t)


def test_toolbar_busy_state_shows_working_dot_and_stop_hint():
    t = _txt(build_toolbar("default", 1200, 0.0143, busy=True,
                           current="notes·delete_note", elapsed=4, tools=3))
    assert "working" in t and "notes·delete_note" in t
    assert "Ctrl-C to stop" in t and "4s" in t
    assert not NO_CYRILLIC.search(t)


def test_toolbar_consent_state():
    t = _txt(build_toolbar("default", 0, 0.0, consent=True))
    assert "approve?" in t and "Enter to send" in t
    assert not NO_CYRILLIC.search(t)


def test_toolbar_busy_spinner_animates_with_elapsed():
    a = _txt(build_toolbar("default", 0, 0.0, busy=True, elapsed=0.0))
    b = _txt(build_toolbar("default", 0, 0.0, busy=True, elapsed=0.4))
    assert a != b                       # the spinner frame advances as time passes
    assert "working" in a and "working" in b


def test_toolbar_mode_colored_per_mode():
    # each mode carries its own style class so the eye catches the mode at a glance
    def _mode_style(mode):
        for style, seg in build_toolbar(mode, 0, 0.0):
            if seg == mode:
                return style
        return ""
    assert _mode_style("autopilot") == "class:tb.mode.autopilot"
    assert _mode_style("plan") == "class:tb.mode.plan"
    assert _mode_style("default") == "class:tb.mode.default"


def test_output_pane_captures_colored_text():
    # The pane's Rich Console renders into a buffer as ANSI (colours preserved
    # for the FormattedTextControl to show); construction works headlessly.
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.console.print("[bold red]Error[/] ok")
    out = pane.dump()
    assert "Error" in out and "ok" in out
    assert "\x1b[" in out          # ANSI colour escapes preserved for the pane
