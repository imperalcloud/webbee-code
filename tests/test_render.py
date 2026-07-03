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
