from rich.console import Console
from webbee.render import RichSink


def _sink():
    console = Console(record=True, width=80, force_terminal=False)
    return RichSink(console=console, live_enabled=False, input_fn=lambda p: "yes", clock=lambda: 0.0)


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
    assert reply == "ага давай"  # trimmed, but NOT interpreted


def test_usage_accumulates_credits():
    s = _sink()
    s.usage(120, 3400, 120)
    s.usage(80, 2000, 200)
    assert s.credits == 200


def test_usage_accumulates_delta_when_cumulative_omitted():
    s = _sink()
    s.usage(120, 3400, None)
    s.usage(80, 2000, None)
    assert s.credits == 200


def test_usage_zero_cumulative_is_authoritative_not_drift():
    s = _sink()
    s.usage(120, 3400, 120)
    s.usage(0, 0, 0)
    assert s.credits == 0


def test_end_turn_without_begin_turn_shows_zero_elapsed():
    s = _sink()
    s.end_turn("x")
    out = s.console.export_text()
    assert "◷ 0.0s" in out


def test_begin_turn_twice_does_not_raise():
    s = _sink()
    s.begin_turn()
    s.begin_turn()


def test_end_turn_renders_final_markdown_and_summary():
    s = _sink()
    s.begin_turn()
    s.tool_start("bash", {"command": "pytest"})
    s.tool_result("bash", True, "12 passed")
    s.usage(200, 5000, 200)
    s.end_turn("**Готово.** Тесты зелёные.")
    out = s.console.export_text()
    assert "Готово" in out
    assert "200" in out            # credits in summary
    assert "credits" in out.lower() or "кредит" in out.lower()


def test_panel_release_shows_url():
    s = _sink()
    s.panel_release("https://panel.imperal.io/x", "Это стоит денег")
    out = s.console.export_text()
    assert "panel.imperal.io/x" in out


def test_clear_resets_credits_and_tools():
    s = _sink()
    s.credits = 5
    s._tools = 3
    s.clear()
    assert s.credits == 0
    assert s._tools == 0


def test_abort_does_not_raise_without_active_live():
    s = _sink()
    s.abort()  # no live running — must be a clean no-op
