from webbee.commands import dispatch, CommandContext, SlashResult


def _ctx(**kw):
    base = dict(mode="default", workspace="/w", version="0.1.0", surface="terminal",
                logged_in=True, credits=42, git_branch="main")
    base.update(kw)
    return CommandContext(**base)


def test_non_slash_is_not_handled():
    r = dispatch("напиши тест", _ctx())
    assert r == SlashResult(handled=False)


def test_exit_and_quit():
    for cmd in ("/exit", "/quit"):
        r = dispatch(cmd, _ctx())
        assert r.handled and r.exit


def test_help_lists_commands():
    r = dispatch("/help", _ctx())
    assert r.handled and r.action == "help"
    for c in ("/login", "/logout", "/clear", "/mode", "/cost", "/status", "/exit"):
        assert c in r.message


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
    r = dispatch("/status", _ctx(credits=99, git_branch="dev"))
    assert r.action == "status"
    assert "terminal" in r.message and "99" in r.message and "dev" in r.message and "0.1.0" in r.message


def test_cost_and_usage_alias():
    assert dispatch("/cost", _ctx()).action == "cost"
    assert dispatch("/usage", _ctx()).action == "cost"


def test_clear_login_logout_actions():
    assert dispatch("/clear", _ctx()).action == "clear"
    assert dispatch("/login", _ctx()).action == "login"
    assert dispatch("/logout", _ctx()).action == "logout"


def test_unknown_slash_is_handled_with_hint():
    r = dispatch("/frobnicate", _ctx())
    assert r.handled and "/help" in r.message
