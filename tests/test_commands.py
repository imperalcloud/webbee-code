import re

from webbee.commands import dispatch, CommandContext, SlashResult

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def _ctx(**kw):
    base = dict(mode="default", workspace="/w", version="0.1.0", surface="terminal",
                logged_in=True, session_tokens=42, session_credits=0.0, git_branch="main")
    base.update(kw)
    return CommandContext(**base)


def test_non_slash_is_not_handled():
    r = dispatch("write a test", _ctx())
    assert r == SlashResult(handled=False)


def test_exit_and_quit():
    for cmd in ("/exit", "/quit"):
        r = dispatch(cmd, _ctx())
        assert r.handled and r.exit


def test_help_lists_commands():
    r = dispatch("/help", _ctx())
    assert r.handled and r.action == "help"
    for c in ("/login", "/logout", "/clear", "/mode", "/cost", "/status", "/sessions", "/logout-others", "/exit"):
        assert c in r.message


def test_help_is_english():
    r = dispatch("/help", _ctx())
    for c in ("/login", "/logout", "/clear", "/mode", "/cost", "/status", "/sessions", "/logout-others", "/exit"):
        assert c in r.message
    assert not NO_CYRILLIC.search(r.message)


def test_mode_switch_valid():
    r = dispatch("/mode autopilot", _ctx())
    assert r.handled and r.action == "mode" and r.new_mode == "autopilot"


def test_mode_shows_current_when_no_arg():
    r = dispatch("/mode", _ctx(mode="plan"))
    assert r.handled and r.new_mode is None and "plan" in r.message


def test_mode_rejects_unknown():
    r = dispatch("/mode turbo", _ctx())
    assert r.handled and r.new_mode is None and "turbo" in r.message


def test_status_reports_state():
    r = dispatch("/status", _ctx(session_tokens=99, git_branch="dev"))
    assert r.action == "status"
    assert "terminal" in r.message and "99" in r.message and "dev" in r.message and "0.1.0" in r.message


def test_status_english():
    r = dispatch("/status", _ctx(session_tokens=1500))
    assert "terminal" in r.message and "1500" in r.message and "0.1.0" in r.message
    assert not NO_CYRILLIC.search(r.message)


def test_cost_and_usage_alias():
    assert dispatch("/cost", _ctx()).action == "cost"
    assert dispatch("/usage", _ctx()).action == "cost"


def test_cost_shows_tokens():
    r = dispatch("/cost", _ctx(session_tokens=1500, session_credits=0.012))
    assert r.action == "cost"
    assert "1500" in r.message and "token" in r.message.lower()


def test_cost_shows_session_total():
    r = dispatch("/cost", _ctx(session_tokens=350, session_credits=0.03))
    assert "350" in r.message and "token" in r.message.lower()
    assert not NO_CYRILLIC.search(r.message)


def test_cost_english_tokens():
    r = dispatch("/cost", _ctx(session_tokens=1500, session_credits=0.012))
    assert r.action == "cost" and "1500" in r.message and "token" in r.message.lower()
    assert not NO_CYRILLIC.search(r.message)


def test_status_shows_tokens():
    r = dispatch("/status", _ctx(session_tokens=1500))
    assert "1500" in r.message


def test_clear_login_logout_actions():
    assert dispatch("/clear", _ctx()).action == "clear"
    assert dispatch("/login", _ctx()).action == "login"
    assert dispatch("/logout", _ctx()).action == "logout"


def test_sessions_commands():
    assert dispatch("/sessions", _ctx()).action == "sessions"
    r = dispatch("/sessions revoke 2", _ctx())
    assert r.action == "sessions_revoke" and r.arg == "2"
    assert dispatch("/sessions revoke", _ctx()).action == "sessions_revoke"  # no index -> arg ""
    assert dispatch("/logout-others", _ctx()).action == "logout_others"


def test_unknown_slash_is_handled_with_hint():
    r = dispatch("/frobnicate", _ctx())
    assert r.handled and "/help" in r.message


def test_mode_and_unknown_english():
    assert not NO_CYRILLIC.search(dispatch("/mode", _ctx()).message)
    assert not NO_CYRILLIC.search(dispatch("/mode turbo", _ctx()).message)
    assert not NO_CYRILLIC.search(dispatch("/frobnicate", _ctx()).message)


def _anyctx():
    from webbee.commands import CommandContext
    return CommandContext(mode="default", workspace="/w", version="x", surface="terminal",
                          logged_in=True, session_tokens=0, session_credits=0,
                          git_branch="main")


def test_checkpoints_command_dispatches():
    from webbee.commands import dispatch
    res = dispatch("/checkpoints", _anyctx())
    assert res.handled and res.action == "checkpoints"


def test_rollback_command_carries_ref():
    from webbee.commands import dispatch
    res = dispatch("/rollback cp-3", _anyctx())
    assert res.handled and res.action == "rollback" and res.arg == "cp-3"


def test_help_mentions_the_time_machine():
    from webbee.commands import dispatch
    res = dispatch("/help", _anyctx())
    assert "/checkpoints" in res.message and "/rollback" in res.message


def test_notify_command_dispatches():
    from webbee.commands import dispatch
    assert dispatch("/notify", _anyctx()).action == "notify"
    r = dispatch("/notify tg", _anyctx())
    assert r.action == "notify" and r.arg == "tg"


def test_help_mentions_notify():
    from webbee.commands import dispatch
    assert "/notify" in dispatch("/help", _anyctx()).message


# ── /queue — manage the dock's type-ahead queue (0.3.12) ─────────────────────
# The queue snapshot rides in CommandContext.queued (threaded by the repl the
# same way /status reads session state); dispatch stays pure.


def test_queue_lists_pending_numbered_in_order():
    r = dispatch("/queue", _ctx(queued=("fix the tests", "deploy it")))
    assert r.handled and r.action == "queue"
    assert "1. fix the tests" in r.message and "2. deploy it" in r.message
    assert r.message.index("fix the tests") < r.message.index("deploy it")
    assert not NO_CYRILLIC.search(r.message)


def test_queue_empty_shows_hint():
    r = dispatch("/queue", _ctx())
    assert r.handled and r.action == "queue" and "empty" in r.message.lower()


def test_queue_clear_reports_drop_count():
    r = dispatch("/queue clear", _ctx(queued=("a", "b", "c")))
    assert r.handled and r.action == "queue_clear"
    assert "3 dropped" in r.message


def test_queue_clear_when_already_empty():
    r = dispatch("/queue clear", _ctx())
    assert r.handled and r.action == "queue_clear"
    assert "already empty" in r.message.lower()


def test_help_mentions_queue():
    assert "/queue" in dispatch("/help", _ctx()).message


# ── /new /tab /close /tabs — tab lifecycle commands (W4a Task 5) ────────────


def test_new_command_dispatches_with_optional_path():
    r = dispatch("/new", _ctx())
    assert r.handled and r.action == "new_tab" and r.arg == ""
    r2 = dispatch("/new ../other-repo", _ctx())
    assert r2.action == "new_tab" and r2.arg == "../other-repo"


def test_tab_command_dispatches_with_index_arg():
    r = dispatch("/tab 2", _ctx())
    assert r.handled and r.action == "tab_switch" and r.arg == "2"
    r2 = dispatch("/tab", _ctx())
    assert r2.action == "tab_switch" and r2.arg == ""   # repl reports "no such tab"


def test_close_command_dispatches():
    r = dispatch("/close", _ctx())
    assert r.handled and r.action == "tab_close"


def test_tabs_command_dispatches():
    r = dispatch("/tabs", _ctx())
    assert r.handled and r.action == "tabs_list"


def test_help_mentions_tab_commands():
    msg = dispatch("/help", _ctx()).message
    for c in ("/new", "/tab", "/close", "/tabs"):
        assert c in msg
    assert not NO_CYRILLIC.search(msg)


# ── /rename — W4c T3: tabs that name themselves ──────────────────────────────

def test_rename_command_carries_the_name_verbatim():
    r = dispatch("/rename billing fix", _ctx())
    assert r.handled and r.action == "rename" and r.arg == "billing fix"


def test_rename_command_with_no_arg_carries_empty_string():
    r = dispatch("/rename", _ctx())
    assert r.handled and r.action == "rename" and r.arg == ""


def test_help_mentions_rename():
    assert "/rename" in dispatch("/help", _ctx()).message
