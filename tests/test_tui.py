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
    assert "tab" in t.lower()          # Shift+Tab hint
    assert not NO_CYRILLIC.search(t)
