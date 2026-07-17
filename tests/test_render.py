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

def test_welcome_full_account_essentials_only():
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
    assert "v@imperal.io" in out and "notvallium" in out and "pro plan" in out
    # trimmed: vanity rows are gone, and a healthy plan doesn't shout "active"
    assert "explorer" not in out and "Apr 2026" not in out
    assert "renews" not in out and "active" not in out
    assert "/help" in out
    assert not NO_CYRILLIC.search(out)   # English UI only


def test_welcome_privacy_line_and_hint():
    from webbee.account import Account
    from webbee.render import WELCOME_HINT, WELCOME_PRIVACY, WELCOME_PRIVACY_DETAIL
    s = _sink()
    s.welcome(Account(signed_in=True, email="v@imperal.io"), "/x", "terminal")
    out = s.console.export_text()
    # the privacy promise renders verbatim (claims-disciplined copy, see render.py)
    assert "🔒" in out
    assert "Your work stays yours — never sold, never training data." in out
    assert "PII is masked before it reaches the model." in out
    # the old verbose tip collapsed into ONE crisp hint line
    assert "Type a task" in out and "/help" in out and "Ctrl-D" in out
    assert "credits run low" not in out and "--once" not in out
    # constants stay in sync with what actually renders
    for const in (WELCOME_PRIVACY, WELCOME_PRIVACY_DETAIL, WELCOME_HINT):
        assert const.strip() in out
    assert not NO_CYRILLIC.search(out)   # English UI only


def test_welcome_plan_status_shown_only_when_abnormal():
    from webbee.account import Account
    s = _sink()
    s.welcome(Account(signed_in=True, email="v@imperal.io", plan="pro",
                      plan_status="past_due"), "/x", "terminal")
    out = s.console.export_text()
    assert "pro plan (past_due)" in out


def test_welcome_signed_out_minimal():
    from webbee.account import Account
    s = _sink()
    s.welcome(Account(signed_in=False), "/x", "terminal")
    out = s.console.export_text()
    assert "i m p e r a l . i o" in out  # letter-spaced brand caption, per Target welcome
    assert "/login" in out
    # trust line shows for everyone, signed in or not
    assert "never sold, never training data" in out
    assert not NO_CYRILLIC.search(out)   # English UI only


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


def test_foreign_turn_tagged_line_no_turn_accounting():
    # A cross-surface line is DISPLAY-ONLY: tagged by origin surface, empty
    # tag for terminal/replay-own, and it must NOT touch the terminal's own
    # turn accounting (_tools/tokens/_busy) -- recon §3's contamination risk.
    s = _sink()
    s.begin_turn()
    tools, tokens, busy = s._tools, s.tokens, s._busy
    s.foreign_turn("telegram", "assistant", "done on telegram")
    s.foreign_turn("terminal", "user", "fix the tests")
    s.foreign_turn("", "assistant", "stale line")
    out = s.console.export_text()
    assert "🐝 webbee [telegram]: done on telegram" in out
    assert "you: fix the tests" in out                  # terminal -> no tag
    assert "[terminal]" not in out
    assert "🐝 webbee: stale line" in out               # empty surface -> no tag
    assert (s._tools, s.tokens, s._busy) == (tools, tokens, busy)
    assert not NO_CYRILLIC.search(out)


def test_consent_dismissed_resets_prompt_state_and_prints_note():
    # Liveness A: a consent answered on ANOTHER surface retires the pinned
    # prompt — the armed Future is cancelled (consent_pending() -> False, so
    # the toolbar leaves `approve? y/n`) and ONE note-style line explains why
    # the prompt vanished. A late local Enter is then a harmless no-op.
    async def _t():
        s = _sink()
        s._consent = asyncio.get_running_loop().create_future()
        s._consent_summary = "bash"
        assert s.consent_pending() is True
        s.consent_dismissed("↩ answered from another surface")
        assert s.consent_pending() is False
        assert s._consent is None and s._consent_summary == ""
        s.resolve_consent("y")   # late keypress after dismissal -> no-op
        assert "answered from another surface" in s.console.export_text()
    asyncio.run(_t())


def test_foreign_turn_strips_control_bytes():
    # Kernel-relayed text is untrusted -- same _clean rule as note()/tool lines.
    s = _sink()
    s.foreign_turn("tele\x1b[2Jgram", "assistant", "hi\x1b]0;pwned\x07 there")
    out = s.console.export_text()
    assert "\x1b[2J" not in out and "pwned" not in out
    assert "[telegram]" in out and "hi there" in out


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


# ---- control-byte sanitization of untrusted content (0.3.3) -----------------
# Tool output / kernel-relayed text must never carry raw ESC sequences into the
# terminal — \x1b[?1003h in a printed summary would silently flip the user's
# terminal into any-event mouse tracking (the mouse-garbage bug's evil twin).

_INJ = "ok\x1b[?1003h\x1b]0;pwned\x07 done"


def test_note_strips_escape_sequences():
    s = _sink()
    s.note(_INJ)
    out = s.console.export_text()
    assert "\x1b[?1003h" not in out and "pwned" not in out
    assert "ok" in out and "done" in out


def test_tool_result_strips_escape_sequences():
    s = _sink()
    s.tool_start("bash", {"command": "cat file"})
    s.tool_result("bash", True, _INJ)
    out = s.console.export_text()
    assert "\x1b[?1003h" not in out
    assert "ok" in out


def test_end_turn_strips_escape_sequences_keeps_newlines():
    s = _sink()
    s.begin_turn()
    s.end_turn("line one\x1b[?1003h\n\nline two")
    out = s.console.export_text()
    assert "\x1b[?1003h" not in out
    assert "line one" in out and "line two" in out


def test_progress_and_thinking_strip_escapes():
    s = _sink()
    s.progress(_INJ)
    s.thinking(_INJ)
    out = s.console.export_text()
    assert "\x1b[?1003h" not in out and "pwned" not in out


# ── queued_run — the drain marker (0.3.12; the `⋯ queued:` scrollback echo was
# replaced by the LIVE queue panel in 0.3.13 — queue visibility now lives in
# webbee.queue_panel, tested in test_tui.py; the transcript stays real-turns-only
# plus this one-line drain provenance mark) ────────────────────────────────────


def _rec_sink():
    from rich.console import Console

    from webbee.render import RichSink
    c = Console(record=True, width=100)
    return RichSink(console=c, live_enabled=False, input_fn=lambda p: "", clock=lambda: 0.0), c


def test_sink_has_no_queued_echo_anymore():
    # The static scrollback echo is GONE (it scrolled away, duplicated and went
    # stale when edited) — the live panel above the input owns queue visibility.
    s, _ = _rec_sink()
    assert not hasattr(s, "queued_echo")


def test_queued_run_marker_announces_the_drain():
    s, c = _rec_sink()
    s.queued_run(2)
    out = c.export_text()
    assert "▶ running queued message" in out and "2 still queued" in out


def test_queued_run_marker_omits_zero_remaining():
    s, c = _rec_sink()
    s.queued_run(0)
    out = c.export_text()
    assert "▶ running queued message" in out and "still queued" not in out


# ── todos — the full Claude-Code-style checklist (0.3.12) ─────────────────────


def test_todos_renders_full_checklist_with_status_glyphs():
    s, c = _rec_sink()
    s.todos([
        {"content": "map the repo", "status": "completed"},
        {"content": "fix the bug", "status": "in_progress"},
        {"content": "run tests", "status": "pending"},
    ], 3, 1)
    out = c.export_text()
    assert "📋 Todos (1/3)" in out
    assert "✓" in out and "▶" in out and "○" in out       # one glyph per status
    # every item visible, list ORDER preserved
    assert out.index("map the repo") < out.index("fix the bug") < out.index("run tests")


def test_todos_glyph_sits_on_its_item_line():
    s, c = _rec_sink()
    s.todos([{"content": "only item", "status": "in_progress"}], 1, 0)
    lines = c.export_text().splitlines()
    assert any("▶" in ln and "only item" in ln for ln in lines)
    assert not NO_CYRILLIC.search(c.export_text())        # English UI only


def test_todos_malformed_items_and_counts_never_raise():
    s, c = _rec_sink()
    s.todos(["garbage", None, {"status": "pending"}, {"content": "real", "status": "weird"}],
            "x", None)                                    # bad counts too
    out = c.export_text()
    assert "📋 Todos" in out
    assert "real" in out and "garbage" not in out         # malformed entries skipped
    assert "○" in out                                     # unknown status -> pending glyph


def test_todos_items_not_a_list_never_raises():
    s, c = _rec_sink()
    s.todos("nope", 2, 1)                                 # type: ignore[arg-type]
    assert "📋 Todos (1/2)" in c.export_text()


def test_todos_empty_list_renders_minimal_header():
    s, c = _rec_sink()
    s.todos([], 0, 0)
    assert "📋 Todos (0/0)" in c.export_text()


def test_todos_strips_control_bytes_from_content():
    s, c = _rec_sink()
    s.todos([{"content": "evil\x1b[?1003hitem", "status": "pending"}], 1, 0)
    out = c.export_text()
    assert "\x1b[?1003h" not in out and "evilitem" in out


# ── 0.3.14: remote-queued panel state (task_queued/task_dequeued hooks) ───────
# The sink OWNS the cross-surface queue-panel state: remote_queued appends a
# tagged display-only row, remote_dequeued removes it (iid first, oldest
# same-origin fallback), and turn end / abort clears the lot -- the kernel
# session's queue only exists while a run is live, so leftovers would be
# phantom rows. Always mutate IN PLACE: the dock panel holds this list object.

def test_remote_queued_appends_and_dequeued_removes_by_iid():
    s = _sink()
    rows = s.remote_pending                       # the panel's list object
    s.remote_queued("telegram", "fix the tests", "i1")
    s.remote_queued("web-panel", "then docs", "i2")
    assert rows == [{"origin": "telegram", "text": "fix the tests", "iid": "i1"},
                    {"origin": "web-panel", "text": "then docs", "iid": "i2"}]
    s.remote_dequeued("telegram", "i1")
    assert rows == [{"origin": "web-panel", "text": "then docs", "iid": "i2"}]
    assert s.remote_pending is rows               # never rebound


def test_remote_dequeued_falls_back_to_oldest_same_origin():
    # An older kernel / a lost announce can leave the iid unmatched: the
    # kernel queue is FIFO, so the OLDEST same-origin row is the one that
    # just started. Nothing matched at all -> no-op (never a crash).
    s = _sink()
    s.remote_queued("telegram", "first", "i1")
    s.remote_queued("web-panel", "other", "i2")
    s.remote_queued("telegram", "second", "i3")
    s.remote_dequeued("telegram", "")             # no iid -> oldest telegram row
    assert [r["iid"] for r in s.remote_pending] == ["i2", "i3"]
    s.remote_dequeued("discord", "zzz")           # nothing matches -> no-op
    assert [r["iid"] for r in s.remote_pending] == ["i2", "i3"]


def test_remote_rows_sanitized_like_all_untrusted_text():
    s = _sink()
    s.remote_queued("tele\x1b[2Jgram", "do\x1b]0;pwned\x07 it", "i1")
    row = s.remote_pending[0]
    assert row["origin"] == "telegram" and row["text"] == "do it"


def test_end_turn_and_abort_clear_remote_rows_in_place():
    s = _sink()
    rows = s.remote_pending
    s.begin_turn(); s.remote_queued("telegram", "queued", "i1")
    s.end_turn("done")
    assert rows == [] and s.remote_pending is rows
    s.begin_turn(); s.remote_queued("telegram", "queued again", "i2")
    s.abort()
    assert rows == [] and s.remote_pending is rows


# ── 0.3.16: queue-panel single-source dedup (steer_iid reconciliation) ────────
# The steer_iid minted at enqueue time is the ONE key both legs carry; the
# display layer now consumes it too: a duplicated task_queued frame
# (at-least-once delivery) never doubles a row, and the kernel echo of a
# terminal-injected line REPLACES its local fallback twin — one message,
# one owner (the kernel), one panel row.

def test_remote_queued_ignores_duplicate_frame_same_iid():
    s = _sink()
    s.remote_queued("terminal", "ship it", "i1")
    s.remote_queued("terminal", "ship it", "i1")   # publish retry / SSE resume
    assert [r["iid"] for r in s.remote_pending] == ["i1"]


def test_remote_queued_empty_iid_never_dedups():
    # Legacy kernels emit steer_iid="" — identical re-typed lines must keep
    # appending (text is NOT a dedup key).
    s = _sink()
    s.remote_queued("telegram", "same text", "")
    s.remote_queued("telegram", "same text", "")
    assert len(s.remote_pending) == 2


def test_remote_queued_promotes_local_twin_to_kernel_owned():
    # A failed-LOOKING inject that actually landed leaves a local QueuedLine
    # twin; the kernel echo (same iid) is positive proof it landed — the
    # local row goes, the message shows exactly once (kernel-owned).
    from collections import deque

    from webbee.tui import QueuedLine
    s = _sink()
    lp = deque([QueuedLine("ship it", "i1"), QueuedLine("other", "i9")])
    s.local_pending = lp
    s.remote_queued("terminal", "ship it", "i1")
    assert [str(x) for x in lp] == ["other"]
    assert [r["iid"] for r in s.remote_pending] == ["i1"]


def test_remote_dequeued_nonempty_iid_unmatched_is_noop():
    # task_dequeued twins (the kernel emits one per pop, dedup-dropped twins
    # included) must never eat a DIFFERENT same-origin row: the origin-FIFO
    # fallback is legacy-only (empty iid).
    s = _sink()
    s.remote_queued("terminal", "a", "i1")
    s.remote_queued("terminal", "b", "i2")
    s.remote_dequeued("terminal", "i-unknown")
    assert [r["iid"] for r in s.remote_pending] == ["i1", "i2"]


# ── 0.3.14: the terminal-local yes/no confirm (autopilot safe-asymmetry) ──────

def test_ask_yes_no_sync_fallback_strict_yes_only():
    # No dock -> the injected sync reader answers. STRICT: only y/yes is True.
    for reply, want in (("y", True), ("YES", True), ("n", False),
                        ("", False), ("да", False), ("approve", False)):
        s = RichSink(console=Console(record=True, width=80, force_terminal=False),
                     live_enabled=False, input_fn=lambda p, r=reply: r,
                     clock=lambda: 0.0)
        assert asyncio.run(s.ask_yes_no("switch to autopilot — allow? [y/n]")) is want
        assert s.consent_pending() is False
    out = s.console.export_text()
    assert "allow?" in out and not NO_CYRILLIC.search(out)


def test_ask_yes_no_dock_path_resolves_via_the_pinned_input(monkeypatch):
    # With a dock running, ask_yes_no arms the SAME future the consent prompt
    # uses -- the Enter handler's resolve_consent() is the answer path.
    import prompt_toolkit.application as PA
    monkeypatch.setattr(PA, "get_app_or_none", lambda: object())
    s = _sink()

    async def _t():
        task = asyncio.ensure_future(s.ask_yes_no("telegram asks — allow? [y/n]"))
        await asyncio.sleep(0)
        assert s.consent_pending() is True     # toolbar flips to the reply state
        s.resolve_consent("y")
        return await task

    assert asyncio.run(_t()) is True
    assert s.consent_pending() is False and s._consent is None


def test_ask_yes_no_timeout_declines_and_disarms(monkeypatch):
    # The person may simply be away (that IS the remote-control scenario):
    # the prompt times out to a DECLINE and fully disarms the input.
    import prompt_toolkit.application as PA
    monkeypatch.setattr(PA, "get_app_or_none", lambda: object())
    s = _sink()

    async def _t():
        return await s.ask_yes_no("allow? [y/n]", timeout=0.01)

    assert asyncio.run(_t()) is False
    assert s.consent_pending() is False and s._consent is None


def test_ask_yes_no_input_error_declines():
    # A broken reader (or ^C at the sync prompt) must decline, never raise.
    def _boom(p):
        raise RuntimeError("tty gone")
    s = RichSink(console=Console(record=True, width=80, force_terminal=False),
                 live_enabled=False, input_fn=_boom, clock=lambda: 0.0)
    assert asyncio.run(s.ask_yes_no("allow? [y/n]")) is False


# ── 0.3.15: sticky todo panel state (RichSink.current_todos) ──────────────────
# The sink OWNS the checklist state the dock's sticky todo panel renders:
# todos() replaces `current_todos` IN PLACE (the panel holds the list object)
# on every todo frame. In the DOCK (on_output wired) the inline scroll-away
# render is gone — the live panel is the renderer and end_turn prints the
# final checklist ONCE per touching turn as the scrollback record, after
# which the panel PERSISTS (sticky, always-on) into idle. Headless/non-dock
# sinks keep the full inline render (they have no panel). /clear resets it.

def _dock_sink():
    from rich.console import Console

    from webbee.render import RichSink
    c = Console(record=True, width=100)
    return RichSink(console=c, live_enabled=False, input_fn=lambda p: "",
                    clock=lambda: 0.0, on_output=lambda: None), c


def test_dock_todos_update_panel_state_in_place_without_inline_print():
    s, c = _dock_sink()
    rows = s.current_todos                        # the panel's list object
    s.todos([{"content": "map the repo", "status": "completed"},
             {"content": "fix the bug", "status": "in_progress"}], 2, 1)
    assert rows == [{"content": "map the repo", "status": "completed"},
                    {"content": "fix the bug", "status": "in_progress"}]
    s.todos([{"content": "fix the bug", "status": "completed"}], 2, 2)
    assert rows == [{"content": "fix the bug", "status": "completed"}]
    assert s.current_todos is rows                # never rebound — mutated in place
    assert "📋 Todos" not in c.export_text()      # the panel renders it, not the feed


def test_dock_todos_sanitize_and_skip_malformed_before_the_panel():
    s, _ = _dock_sink()
    s.todos(["garbage", None, {"status": "pending"},
             {"content": "evil\x1b[?1003hitem", "status": "pending"}], "x", None)
    assert s.current_todos == [{"content": "evilitem", "status": "pending"}]


def test_dock_end_turn_records_checklist_once_then_panel_persists():
    s, c = _dock_sink()
    rows = s.current_todos
    s.begin_turn()
    s.todos([{"content": "ship it", "status": "in_progress"}], 3, 1)
    s.end_turn("done")
    out = c.export_text(clear=False)
    assert "📋 Todos (1/3)" in out and "▶" in out    # ONE scrollback record
    assert rows and s.current_todos is rows          # STICKY — panel survives idle
    # a later turn that never touches the list re-records NOTHING
    s.begin_turn(); s.end_turn("again")
    assert c.export_text().count("📋 Todos") == 1


def test_dock_interrupted_turn_still_records_the_checklist():
    # Ctrl-C path: repl calls abort() then end_turn("") — the record must
    # still land in the scrollback and the panel must survive (always-on).
    s, c = _dock_sink()
    s.begin_turn()
    s.todos([{"content": "half done", "status": "in_progress"}], 2, 0)
    s.abort()
    s.end_turn("")
    assert "📋 Todos (0/2)" in c.export_text()
    assert s.current_todos                            # abort never wipes the plan


def test_clear_resets_todo_panel_state_in_place():
    s, _ = _dock_sink()
    rows = s.current_todos
    s.todos([{"content": "x", "status": "pending"}], 1, 0)
    s.clear()
    assert rows == [] and s.current_todos is rows
    s.begin_turn(); s.end_turn("t")                   # cleared list → no stale record


def test_headless_todos_keep_the_full_inline_render():
    s, c = _rec_sink()                                # no on_output → no dock panel
    s.todos([{"content": "fix the bug", "status": "in_progress"}], 1, 0)
    out = c.export_text()
    assert "📋 Todos (0/1)" in out and "▶" in out     # inline render as before
    assert s.current_todos == [{"content": "fix the bug",
                                "status": "in_progress"}]   # twin state still kept
