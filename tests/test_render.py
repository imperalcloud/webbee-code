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


def test_end_turn_renders_final_markdown_and_summary():
    s = _sink()
    s.begin_turn()
    s.tool_start("bash", {"command": "pytest"})
    s.tool_result("bash", True, "12 passed")
    s.usage(5000, 0.05)
    s.end_turn("**Готово.** Тесты зелёные.")
    out = s.console.export_text()
    assert "Готово" in out
    assert "5.0k" in out            # tokens in summary
    assert "tokens" in out.lower()


def test_panel_release_shows_url():
    s = _sink()
    s.panel_release("https://panel.imperal.io/x", "Это стоит денег")
    out = s.console.export_text()
    assert "panel.imperal.io/x" in out


def test_clear_resets_tokens_and_cost():
    s = _sink()
    s.usage(500, 0.01); s._tools = 2
    s.clear()
    assert s.tokens == 0 and s.cost_usd == 0.0 and s._tools == 0


def test_abort_does_not_raise_without_active_live():
    s = _sink()
    s.abort()  # no live running — must be a clean no-op
