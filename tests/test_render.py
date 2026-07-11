import asyncio
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


def test_login_prompt_shows_code_and_url():
    s = _sink()
    s.login_prompt("WDBK-7Q3M", "https://panel.imperal.io/device")
    out = s.console.export_text()
    assert "WDBK-7Q3M" in out
    assert "panel.imperal.io/device" in out
    assert not NO_CYRILLIC.search(out)   # English UI only


def test_sessions_table_renders():
    s = _sink()
    s.sessions_table([
        {"session_id": "s1", "surface": "cli", "label": "Terminal (webbee)",
         "ip_address": "1.2.3.4", "last_seen_at": "2026-07-04T00:00:00", "current": True},
        {"session_id": "s2", "surface": "web", "label": "Web (Chrome)",
         "ip_address": None, "last_seen_at": "2026-07-03T10:00:00", "current": False},
    ])
    out = s.console.export_text()
    assert "Terminal (webbee)" in out and "Web (Chrome)" in out
    assert "this device" in out and "1.2.3.4" in out
    assert not NO_CYRILLIC.search(out)


def test_sessions_table_empty():
    s = _sink()
    s.sessions_table([])
    assert "none" in s.console.export_text().lower()


def test_ask_consent_relays_raw_input():
    console = Console(record=True, width=80)
    s = RichSink(console=console, live_enabled=False, input_fn=lambda p: "  ага давай  ", clock=lambda: 0.0)
    reply = asyncio.run(s.ask_consent("webbee", "bash", {"command": "ls"}))
    assert reply == "ага давай"  # trimmed, but NOT interpreted — user's own words, any language


def test_usage_sets_tokens_and_cost():
    s = _sink()
    s.usage(1400, 0.0123)
    s.usage(2100, 0.0201)        # cumulative frame — latest wins
    assert s.tokens == 2100
    assert s.credits == 0.0201


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
    assert "0.0s" in out


def test_begin_turn_twice_does_not_raise():
    s = _sink()
    s.begin_turn()
    s.begin_turn()


def test_abort_does_not_raise_without_active_live():
    s = _sink()
    s.abort()  # no live running — must be a clean no-op


# ---- WOW visual system (R1 Task 1) --------------------------------------

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
    assert s.tokens == 2100 and s.credits == 0.02
    s._tools = 3
    s.clear()
    assert s.tokens == 0 and s.credits == 0.0 and s._tools == 0


# ---- WEBBEE CODE welcome + account panel + session /cost (Chunk 1 Task 2) --

def test_welcome_full_account_aligned():
    from webbee.account import Account
    from webbee.banner_art import WEBBEE_CODE
    s = _sink()
    acc = Account(signed_in=True, email="v@imperal.io", nickname="notvallium",
                  plan="pro", plan_status="active", plan_renews="2026-08-01",
                  dev_tier="explorer", member_since="Apr 2026")
    s.welcome(acc, "/home/x/proj", "terminal")
    out = s.console.export_text()
    # WEBBEE_CODE is figlet-style line art (no literal "WEBBEE" letters) —
    # assert a stable fragment of the actual logo constant renders instead.
    assert WEBBEE_CODE.splitlines()[2] in out                  # ascii logo present
    assert "i m p e r a l . i o" in out  # letter-spaced brand caption, per Target welcome
    assert "v@imperal.io" in out and "notvallium" in out
    assert "pro" in out.lower() and "active" in out
    assert "explorer" in out and "Apr 2026" in out
    assert "/help" in out
    assert not NO_CYRILLIC.search(out)


def test_welcome_signed_out_minimal():
    from webbee.account import Account
    s = _sink()
    s.welcome(Account(signed_in=False), "/x", "terminal")
    out = s.console.export_text()
    assert "i m p e r a l . i o" in out  # letter-spaced brand caption, per Target welcome and "/login" in out


def test_session_credits_accumulates_across_turns():
    s = _sink()
    s.begin_turn(); s.usage(100, 0.01); s.end_turn("a")
    s.begin_turn(); s.usage(250, 0.02); s.end_turn("b")   # per-turn cumulative frames
    assert s.session_tokens == 350
    assert abs(s.session_credits - 0.03) < 1e-9


def test_turn_footer_shows_session_total():
    s = _sink()
    s.begin_turn(); s.usage(100, 0.01); s.end_turn("a")
    s.begin_turn(); s.usage(250, 0.02); s.end_turn("b")
    out = s.console.export_text()
    assert "session" in out.lower() and "350" in out


def test_clear_resets_session_totals():
    s = _sink()
    s.begin_turn(); s.usage(100, 0.01); s.end_turn("a")
    s.clear()
    assert s.session_tokens == 0 and s.session_credits == 0.0


# ---- P0 de-clutter + hierarchy -------------------------------------------

def test_salient_arg_prefers_human_fields():
    from webbee.render import _salient_arg
    assert _salient_arg({"note_id": "abc", "title": "Q3 budget"}) == "Q3 budget"
    assert _salient_arg({"path": "auth.py"}) == "auth.py"
    assert _salient_arg({"command": "ls -la"}) == "ls -la"
    assert _salient_arg({"note_id": "abc"}) == "abc"      # falls back to id
    assert _salient_arg({}) == ""
    assert _salient_arg("nope") == ""


def test_end_turn_footer_has_no_rule_bar():
    s = _sink()
    s.begin_turn(); s.usage(1200, 0.0143); s.end_turn("done")
    out = s.console.export_text()
    assert "─────" not in out                    # the heavy full-width Rule is gone
    assert "session" in out.lower() and "1.2k" in out


def test_consent_shows_human_summary_not_dict():
    console = Console(record=True, width=90)
    s = RichSink(console=console, live_enabled=False,
                 input_fn=lambda p: "yes", clock=lambda: 0.0)
    asyncio.run(s.ask_consent("notes", "delete_note", {"note_id": "c93dc86b", "title": "Q3 budget"}))
    out = console.export_text()
    assert "Q3 budget" in out                     # human-readable salient arg
    assert "note_id" not in out and "{" not in out  # no raw dict dump
    assert "notes·delete_note" in out


def test_user_echo_is_unboxed_line():
    s = _sink()
    s.user_echo("delete my Q3 note")
    out = s.console.export_text()
    assert "❯ delete my Q3 note" in out
    assert "╭" not in out and "│" not in out and "╰" not in out   # NOT the input box


def test_answer_marker_shows_webbee_name():
    s = _sink()
    s.begin_turn(); s.end_turn("here is the answer")
    out = s.console.export_text()
    assert "🐝 Webbee" in out            # name + bee, not a bare bee icon


def test_status_bottom_counter_is_session_total():
    s = _sink()
    s.begin_turn(); s.usage(100, 0.01); s.end_turn("a")     # session = 100
    s.begin_turn(); s.usage(250, 0.02)                       # mid-turn (busy)
    assert s.status()["tokens"] == 350                        # live session total incl. in-flight
    s.end_turn("b")                                           # session = 350
    assert s.status()["tokens"] == 350                        # idle: session total, no double-count


# ---- step drill-down (Task 20 P1b) ---------------------------------------

def test_step_detail_renders_facts_and_previews():
    s = _sink()
    s.step_detail({"ok": True, "app_id": "mail", "tool": "list_messages",
                   "duration_ms": 1234, "trace_id": "abc123",
                   "args": {"preview": "{'folder': 'INBOX'}"},
                   "result": {"preview": "5 messages", "truncated": False}})
    out = s.console.export_text()
    assert "mail" in out and "list_messages" in out
    assert "1.2s" in out
    assert "abc123" in out
    assert "INBOX" in out and "5 messages" in out


def test_step_detail_shows_truncated_note():
    s = _sink()
    s.step_detail({"ok": False, "app_id": "", "tool": "bash",
                   "args": {"preview": "ls -la"},
                   "result": {"preview": "x" * 10, "truncated": True, "total_bytes": 9999}})
    out = s.console.export_text()
    assert "truncated" in out and "9999" in out
    assert "✗" in out


def test_plan_blocked_prints_english_hint():
    s = _sink()
    s.plan_blocked("notes.delete_note")
    out = s.console.export_text()
    assert "plan mode" in out.lower()
    assert "notes.delete_note" in out
    assert "shift+tab" in out.lower()
    assert not NO_CYRILLIC.search(out)


# ---- hanging indent on wrapped chrome lines (0.3.2) ------------------------
# A long one-line note/progress/thinking/user-echo used to wrap flush against
# the left screen edge (the 2-space gutter lived only in the first line's
# prefix string). Every visual line must start at the same 2-col gutter.

def _narrow_sink(width=40):
    return RichSink(console=Console(record=True, width=width, force_terminal=False),
                    live_enabled=False, input_fn=lambda p: "", clock=lambda: 0.0)


def _assert_gutter(out: str):
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "nothing rendered"
    for ln in lines:
        assert ln.startswith("  "), f"line lost the gutter: {ln!r}"


def test_note_wraps_with_gutter():
    s = _narrow_sink()
    s.note("Right now we need to move the Matomo extension onto the new "
           "backend path cleanly and rerun the whole test suite afterwards")
    _assert_gutter(s.console.export_text())


def test_progress_wraps_with_gutter():
    s = _narrow_sink()
    s.progress("switching every old analytics call over to the new prefix "
               "without touching any business logic at all")
    _assert_gutter(s.console.export_text())


def test_thinking_wraps_with_gutter():
    s = _narrow_sink()
    s.thinking("bulk replace failed on repeated lines so a scripted targeted "
               "replacement across the tree is faster and safer here")
    _assert_gutter(s.console.export_text())


def test_user_echo_wraps_with_gutter():
    s = _narrow_sink()
    s.user_echo("please migrate the whole extension to the new backend path "
                "and make sure nothing regresses while you are at it")
    _assert_gutter(s.console.export_text())
