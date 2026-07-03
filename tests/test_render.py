import re

from rich.console import Console
from webbee.render import RichSink


def _sink():
    return RichSink(console=Console(record=True, width=80, force_terminal=False),
                     live_enabled=False, input_fn=lambda p: "", clock=lambda: 0.0)


NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def test_implements_turnsink():
    from webbee.events import TurnSink
    assert isinstance(_sink(), TurnSink)


def test_tool_lines_render():
    s = _sink()
    s.begin_turn()
    s.tool_start("read_file", {"path": "auth.py"})
    s.tool_result("read_file", True, "def login(): ...")
    out = s.console.export_text()
    assert "read_file" in out and "auth.py" in out


def test_ask_consent_relays_raw_input():
    console = Console(record=True, width=80)
    s = RichSink(console=console, live_enabled=False, input_fn=lambda p: "  ага давай  ", clock=lambda: 0.0)
    reply = s.ask_consent("webbee", "bash", {"command": "ls"})
    assert reply == "ага давай"  # trimmed, but NOT interpreted — user's own words, any language


def test_usage_sets_tokens_and_cost():
    s = _sink()
    s.usage(1400, 0.0123)
    s.usage(2100, 0.0201)        # cumulative frame — latest wins
    assert s.tokens == 2100
    assert s.cost_usd == 0.0201


def test_status_and_summary_show_tokens_not_credits():
    s = _sink()
    s.begin_turn()
    s.usage(2100, 0.02)
    s.end_turn("**ok**")
    out = s.console.export_text()
    assert "2100" in out or "2.1k" in out          # tokens shown
    assert "tokens" in out.lower() or "tok" in out.lower()


def test_end_turn_without_begin_turn_shows_zero_elapsed():
    s = _sink()
    s.end_turn("x")
    out = s.console.export_text()
    assert "◷ 0.0s" in out


def test_begin_turn_twice_does_not_raise():
    s = _sink()
    s.begin_turn()
    s.begin_turn()


def test_abort_does_not_raise_without_active_live():
    s = _sink()
    s.abort()  # no live running — must be a clean no-op


# ---- WOW visual system (R1 Task 1) --------------------------------------

def test_banner_english_and_context():
    s = _sink()
    s.banner("0.1.0", "/home/x/proj", True, "terminal")
    out = s.console.export_text()
    assert "webbee" in out and "0.1.0" in out
    assert "terminal" in out and "signed in" in out
    assert "/help" in out
    assert not NO_CYRILLIC.search(out)


def test_banner_signed_out_hint():
    s = _sink()
    s.banner("0.1.0", "/x", False, "terminal")
    assert "/login" in s.console.export_text()


def test_action_feed_and_answer_english():
    s = _sink()
    s.begin_turn()
    s.tool_start("read_file", {"path": "pyproject.toml"})
    s.tool_result("read_file", True, "[project]")
    s.usage(12300, 0.01)
    s.end_turn("It's the **webbee** package.")
    out = s.console.export_text()
    assert "read_file" in out and "pyproject.toml" in out
    assert "webbee package" in out                       # markdown answer rendered
    assert "1 action" in out                              # English footer, singular
    assert "12.3k" in out and "tok" in out
    assert "◷" in out
    assert not NO_CYRILLIC.search(out)


def test_footer_pluralises_actions():
    s = _sink()
    s.begin_turn()
    for _ in range(2):
        s.tool_start("bash", {"command": "ls"})
        s.tool_result("bash", True, "ok")
    s.end_turn("done")
    assert "2 actions" in s.console.export_text()


def test_money_card_english():
    s = _sink()
    s.panel_release("https://panel.imperal.io/x", "This costs money")
    out = s.console.export_text()
    assert "panel.imperal.io/x" in out
    assert "browser" in out.lower()
    assert not NO_CYRILLIC.search(out)


def test_ext_action_default_icon():
    s = _sink()
    s.begin_turn()
    s.tool_start("tasks·list_tasks", {})
    s.tool_result("tasks·list_tasks", True, "200 open")
    out = s.console.export_text()
    assert "tasks·list_tasks" in out and "200 open" in out
    assert "⚡" in out


def test_usage_and_clear_still_work():
    s = _sink()
    s.usage(2100, 0.02)
    assert s.tokens == 2100 and s.cost_usd == 0.02
    s._tools = 3
    s.clear()
    assert s.tokens == 0 and s.cost_usd == 0.0 and s._tools == 0
