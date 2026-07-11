import asyncio
import re

from webbee.account import Account
from webbee.repl import run_repl

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


class FakeSink:
    def __init__(self):
        self.turns = []; self.notes = []; self.tokens = 0; self.cost_usd = 0.0; self.mode = None
        self.aborted = False; self.cleared = False
        self.session_tokens = 0; self.session_cost = 0.0
    def begin_turn(self): ...
    def end_turn(self, text): self.turns.append(text)
    def note(self, m): self.notes.append(m)
    def clear(self): self.cleared = True
    def abort(self): self.aborted = True
    def welcome(self, *a, **kw): ...
    def user_echo(self, text): self.echoed = getattr(self, "echoed", []) + [text]
    def sessions_table(self, rows): self.session_tables = getattr(self, "session_tables", []) + [rows]
    # TurnSink no-ops
    def tool_start(self, *a): ...
    def tool_result(self, *a): ...
    def ask_consent(self, *a): return "yes"
    def panel_release(self, *a): ...
    def progress(self, *a): ...
    def usage(self, *a): ...


class FakeAgent:
    def __init__(self): self.tasks = []; self.mode = "default"; self.runs = []
    async def run(self, task, sink, *, marathon=False, goal=""):
        self.tasks.append(task)
        self.runs.append({"task": task, "marathon": marathon, "goal": goal})
        return f"answer:{task}"


class FakeAuth:
    NotLoggedInError = RuntimeError
    def __init__(self, logged_in=True): self._in = logged_in; self.logged_out = False
    async def ensure_access_token(self, cfg):
        if not self._in: raise self.NotLoggedInError("no creds")
        return "tok"
    async def login_device(self, cfg, *, on_prompt=None, open_browser=True):
        if on_prompt:
            on_prompt("WDBK-7Q3M", "https://panel.imperal.io/device",
                      "https://panel.imperal.io/device?code=WDBK-7Q3M")
        self._in = True
        return "u@imperal.io"
    async def logout(self, cfg): self._in = False; self.logged_out = True


class FakeSessions:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"session_id": "s1", "surface": "cli", "label": "Terminal (webbee)", "current": True},
            {"session_id": "s2", "surface": "web", "label": "Web (Chrome)", "current": False},
        ]
        self.revoked = []
        self.others_called = False
    async def list_sessions(self, cfg, tp): return self.rows
    async def revoke_session(self, cfg, tp, sid): self.revoked.append(sid); return True
    async def revoke_others(self, cfg, tp): self.others_called = True; return 2


def _lines(*items):
    it = iter(items)
    def read(prompt=""):
        try: return next(it)
        except StopIteration: raise EOFError
    return read


async def _fake_account_fetcher(cfg, token_provider):
    """Default test double for run_repl's account_fetcher — never touches the
    network (unlike the real fetch_account, which calls out over httpx)."""
    return Account(signed_in=True, email="u@imperal.io")


class _NoopIntel:
    """Default test double for the intel boot -- `_boot` runs it regardless
    of which agent_factory is in play, so without this every test in this
    file would build a REAL IntelService against the actual repo checkout
    (slow, non-hermetic, and it writes into the developer's real
    ~/.cache/webbee/intel). Tests that care about the intel wiring itself
    inject their own `intel_factory` and bypass this default."""
    root = "/noop-root"  # real IntelService always has .root; the watcher is keyed to it (F1)
    def build(self): ...
    def apply_changes(self, paths): ...


def _run(**kw):
    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")
    sink = kw.pop("sink", FakeSink())
    agent = kw.pop("agent", FakeAgent())
    once = kw.pop("once", False)
    asyncio.run(run_repl(cfg, "default", once=once, sink=sink, agent_factory=lambda c, tp, ws, m: agent,
                         read_line=kw.pop("read_line"), auth=kw.pop("auth", FakeAuth()),
                         account_fetcher=kw.pop("account_fetcher", _fake_account_fetcher),
                         sessions_client=kw.pop("sessions_client", FakeSessions()),
                         intel_factory=kw.pop("intel_factory", lambda cfg, ws: _NoopIntel())))
    return sink, agent


def test_task_is_sent_to_agent_and_answer_rendered():
    sink, agent = _run(read_line=_lines("исправь баг", "/exit"))
    assert agent.tasks == ["исправь баг"]
    assert sink.turns == ["answer:исправь баг"]


def test_interactive_default_is_marathon():
    # Marathon is the default: a typed task self-drives (marathon=True) with the
    # task carried as the goal.
    sink, agent = _run(read_line=_lines("build a thing", "/exit"))
    assert agent.runs and agent.runs[0]["marathon"] is True
    assert agent.runs[0]["goal"] == "build a thing"


def test_once_flag_uses_bounded_coding():
    # --once opts back into the bounded, non-marathon coding turn.
    sink, agent = _run(read_line=_lines("build a thing", "/exit"), once=True)
    assert agent.runs and agent.runs[0]["marathon"] is False


def test_exit_command_stops_loop():
    sink, agent = _run(read_line=_lines("/exit"))
    assert agent.tasks == []


def test_blank_lines_skipped():
    sink, agent = _run(read_line=_lines("", "  ", "/exit"))
    assert agent.tasks == []


def test_eof_exits_cleanly():
    sink, agent = _run(read_line=_lines("привет"))  # no /exit → EOF after
    assert agent.tasks == ["привет"]


def test_logout_command_calls_auth():
    auth = FakeAuth()
    sink, agent = _run(read_line=_lines("/logout", "/exit"), auth=auth)
    assert auth.logged_out
    assert any(not NO_CYRILLIC.search(n) for n in sink.notes)


def test_agent_error_is_swallowed_and_loop_continues():
    class RaisingAgent(FakeAgent):
        async def run(self, task, sink, *, marathon=False, goal=""):
            self.tasks.append(task)
            raise RuntimeError("boom")

    agent = RaisingAgent()
    sink, agent = _run(read_line=_lines("do it", "/exit"), agent=agent)
    assert agent.tasks == ["do it"]
    assert any("Error" in n for n in sink.notes)
    assert not any(NO_CYRILLIC.search(n) for n in sink.notes)
    assert sink.turns == []


def test_login_command_calls_auth_and_logs_in():
    auth = FakeAuth(logged_in=False)
    sink, agent = _run(read_line=_lines("/login", "/exit"), auth=auth)
    assert auth._in is True
    assert any(not NO_CYRILLIC.search(n) for n in sink.notes)


def test_login_uses_device_flow_and_renders_prompt():
    # Device-code flow (RFC 8628): repl awaits auth.login_device directly (async,
    # no executor) and on_prompt renders the code + URL into the feed before the
    # poll completes. Confirms the prompt reaches the sink and login persists.
    auth = FakeAuth(logged_in=False)
    sink, agent = _run(read_line=_lines("/login", "/exit"), auth=auth)
    assert auth._in is True
    assert any("WDBK-7Q3M" in n and "panel.imperal.io/device" in n for n in sink.notes)
    assert any("Signed in as u@imperal.io" in n for n in sink.notes)


def test_sessions_list_renders():
    fs = FakeSessions()
    sink, agent = _run(read_line=_lines("/sessions", "/exit"), sessions_client=fs)
    assert getattr(sink, "session_tables", None)          # sessions_table was rendered
    assert sink.session_tables[0] == fs.rows


def test_sessions_revoke_by_index():
    fs = FakeSessions()
    sink, agent = _run(read_line=_lines("/sessions", "/sessions revoke 2", "/exit"), sessions_client=fs)
    assert fs.revoked == ["s2"]                           # #2 = web (not current)
    assert any("Revoked" in n for n in sink.notes)
    assert not any(NO_CYRILLIC.search(n) for n in sink.notes)


def test_sessions_revoke_current_is_guarded():
    fs = FakeSessions()
    sink, agent = _run(read_line=_lines("/sessions", "/sessions revoke 1", "/exit"), sessions_client=fs)
    assert fs.revoked == []                               # #1 = current terminal -> guarded
    assert any("this terminal" in n for n in sink.notes)


def test_logout_others_calls_client():
    fs = FakeSessions()
    sink, agent = _run(read_line=_lines("/logout-others", "/exit"), sessions_client=fs)
    assert fs.others_called
    assert any("other session" in n for n in sink.notes)


def test_mode_command_switches_agent_mode():
    sink, agent = _run(read_line=_lines("/mode autopilot", "/exit"))
    assert agent.mode == "autopilot"


def test_clear_command_clears_sink():
    sink, agent = _run(read_line=_lines("/clear", "/exit"))
    assert sink.cleared is True


def test_ctrl_c_mid_turn_aborts_and_returns_to_prompt():
    class InterruptingAgent(FakeAgent):
        async def run(self, task, sink, *, marathon=False, goal=""):
            self.tasks.append(task)
            raise KeyboardInterrupt

    agent = InterruptingAgent()
    sink, agent = _run(read_line=_lines("go", "/exit"), agent=agent)
    assert agent.tasks == ["go"]
    assert sink.aborted is True
    assert any("Interrupted" in n for n in sink.notes)
    assert not any(NO_CYRILLIC.search(n) for n in sink.notes)
    assert sink.turns == []


def test_welcome_shown_on_start():
    class WSink(FakeSink):
        def __init__(self): super().__init__(); self.welcomed=False
        def welcome(self, account, cwd, surface): self.welcomed=True
    sink=WSink()
    _run(read_line=_lines("/exit"), sink=sink)
    assert sink.welcomed


def test_cycle_mode_updates_agent(monkeypatch):
    # The loop must still run end-to-end with an injected (non-tui) reader —
    # the prod tui.prompt path is never hit here (read_line is not `input`).
    sink, agent = _run(read_line=_lines("/exit"))
    assert agent.tasks == []


def test_next_mode_wired():
    from webbee.tui import next_mode
    assert next_mode("default") == "plan"   # cycle helper is what repl uses


# ── /steps + step drill-down (Task 20 P1b) ────────────────────────────────────

class StepAgent(FakeAgent):
    def __init__(self, steps=None, session_id="sess-1"):
        super().__init__()
        self.steps = steps or []
        self.session_id = session_id


def test_slash_steps_lists_last_turn_steps():
    agent = StepAgent(steps=[{"step_id": "r1", "label": "read_file", "ok": True}])
    sink, agent = _run(read_line=_lines("/steps", "/exit"), agent=agent)
    assert any("read_file" in n for n in sink.notes)


def test_slash_steps_empty_says_no_steps():
    sink, agent = _run(read_line=_lines("/steps", "/exit"), agent=StepAgent())
    assert any("No steps" in n for n in sink.notes)


def test_slash_steps_out_of_range_reports_no_such_step():
    agent = StepAgent(steps=[{"step_id": "r1", "label": "read_file", "ok": True}])
    sink, agent = _run(read_line=_lines("/steps 5", "/exit"), agent=agent)
    assert any("No such step" in n for n in sink.notes)


def test_slash_steps_detail_fetches_and_renders(monkeypatch):
    agent = StepAgent(steps=[{"step_id": "toolu_1", "label": "read_file", "ok": True}])

    async def fake_fetch(cfg, tp, ref):
        assert ref == "terminal:sess-1:toolu_1"
        return {"ok": True, "tool": "read_file"}
    monkeypatch.setattr("webbee.details.fetch_step_detail", fake_fetch)

    class DetailSink(FakeSink):
        def __init__(self):
            super().__init__()
            self.details = []
        def step_detail(self, d): self.details.append(d)

    sink, agent = _run(read_line=_lines("/steps 1", "/exit"), agent=agent, sink=DetailSink())
    assert sink.details == [{"ok": True, "tool": "read_file"}]


def test_slash_steps_detail_unavailable_notes_when_fetch_empty(monkeypatch):
    agent = StepAgent(steps=[{"step_id": "toolu_1", "label": "read_file", "ok": True}])

    async def fake_fetch(cfg, tp, ref):
        return {}
    monkeypatch.setattr("webbee.details.fetch_step_detail", fake_fetch)

    sink, agent = _run(read_line=_lines("/steps 1", "/exit"), agent=agent)
    assert any("unavailable" in n.lower() for n in sink.notes)


# ── CORTEX U1 Task 4: repl-scope IntelService injection wiring ───────────────
# `_boot` builds an IntelService (or an injected fake) off-loop and threads it
# into the DEFAULT agent_factory (custom agent_factory, as used by every test
# above, deliberately ignores it -- unaffected). A base install with no
# tree-sitter/watchfiles extra must still boot cleanly with intel=None.

class _SpyAgent:
    """Stands in for AgentSession -- captures the `intel` kwarg the default
    agent_factory closes over, without needing a real IntelService/network."""
    last_intel = "unset"

    def __init__(self, cfg, tp, ws, mode, intel=None, shadow=None):
        type(self).last_intel = intel
        self.mode = mode
        self.steps = []

    async def run(self, task, sink, *, marathon=False, goal=""):
        return f"answer:{task}"

    async def stop(self): ...


class _FakeIntelService:
    def __init__(self):
        self.built = False
        self.root = "/fake-root"  # real IntelService always has .root (watcher keyed to it, F1)

    def build(self):
        self.built = True

    def apply_changes(self, paths): ...


def test_boot_injects_intel_service_into_agent(monkeypatch):
    monkeypatch.setattr("webbee.repl.AgentSession", _SpyAgent)
    fake = _FakeIntelService()

    def intel_factory(cfg, workspace):
        return fake

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")
    asyncio.run(run_repl(
        cfg, "default", sink=FakeSink(), read_line=_lines("/exit"),
        agent_factory=None, intel_factory=intel_factory,
        auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
        sessions_client=FakeSessions(),
    ))
    assert _SpyAgent.last_intel is fake
    assert fake.built is True


def test_boot_survives_intel_import_failure_base_install(monkeypatch):
    # Simulates a base install (no tree-sitter/watchfiles extra): the intel
    # factory raising must never crash the REPL boot -- the agent still gets
    # constructed, just with intel=None.
    monkeypatch.setattr("webbee.repl.AgentSession", _SpyAgent)

    def broken_intel_factory(cfg, workspace):
        raise ImportError("no tree_sitter")

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")
    sink = FakeSink()
    asyncio.run(run_repl(
        cfg, "default", sink=sink, read_line=_lines("/exit"),
        agent_factory=None, intel_factory=broken_intel_factory,
        auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
        sessions_client=FakeSessions(),
    ))
    assert _SpyAgent.last_intel is None
    assert sink.turns == []  # boot completed cleanly, no crash mid-boot


def test_boot_skips_intel_when_disabled_in_config(monkeypatch):
    monkeypatch.setattr("webbee.repl.AgentSession", _SpyAgent)
    called = []

    def intel_factory(cfg, workspace):
        called.append(1)
        return _FakeIntelService()

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p", intel_enabled=False)
    asyncio.run(run_repl(
        cfg, "default", sink=FakeSink(), read_line=_lines("/exit"),
        agent_factory=None, intel_factory=intel_factory,
        auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
        sessions_client=FakeSessions(),
    ))
    assert called == []                    # intel_factory never invoked
    assert _SpyAgent.last_intel is None


def test_watcher_started_at_intel_root_not_workspace(monkeypatch, tmp_path):
    """F1: IntelService.root is the discovered repo root, which can differ
    from the raw cwd (e.g. the REPL launched from a subdir). The watcher must
    observe intel.root -- otherwise apply_changes' relpath-against-root
    mis-keys the index (e.g. pops 'foo.py' instead of 'pkg/foo.py') and
    sibling/parent files are never watched at all."""
    import os
    monkeypatch.setattr("webbee.repl.AgentSession", _SpyAgent)
    captured = {}

    class _RootedIntel:
        def __init__(self):
            self.root = str(tmp_path)  # deliberately distinct from os.getcwd()

        def build(self): ...
        def apply_changes(self, paths): ...

    async def _dummy(): ...

    def _fake_watch(root, on_change):
        captured["root"] = root
        return _dummy()

    from webbee.intel import watch
    monkeypatch.setattr(watch, "watch_workspace", _fake_watch)

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")
    asyncio.run(run_repl(
        cfg, "default", sink=FakeSink(), read_line=_lines("/exit"),
        agent_factory=None, intel_factory=lambda cfg, ws: _RootedIntel(),
        auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
        sessions_client=FakeSessions(),
    ))
    assert captured.get("root") == str(tmp_path)
    assert captured.get("root") != os.getcwd()
