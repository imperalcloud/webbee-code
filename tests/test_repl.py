import asyncio
import os
import re

from webbee.account import Account
from webbee.repl import (_cancel_all_background, _cancel_slot, _exit_dump,
                         _finish_slot, _live_session_id, _make_session_slot,
                         _slot_ctx, _steer_target, run_marathon, run_repl)
from webbee.slots import SessionSlot, SlotManager, WorkspaceResources

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


class FakeSink:
    def __init__(self):
        self.turns = []; self.notes = []; self.tokens = 0; self.cost_usd = 0.0; self.mode = None
        self.aborted = False; self.cleared = False
        self.session_tokens = 0; self.session_cost = 0.0
    def begin_turn(self): ...
    def end_turn(self, text): self.turns.append(text)
    def note(self, m): self.notes.append(m)
    def todos(self, items, total, done): self.todo_lists = getattr(self, "todo_lists", []) + [(items, total, done)]
    def clear(self): self.cleared = True
    def abort(self): self.aborted = True
    def welcome(self, *a, **kw): ...
    def user_echo(self, text): self.echoed = getattr(self, "echoed", []) + [text]
    def queued_run(self, remaining): self.queued_runs = getattr(self, "queued_runs", []) + [remaining]
    def mark_turn_failed(self): self.turn_failed_marks = getattr(self, "turn_failed_marks", 0) + 1
    def sessions_table(self, rows): self.session_tables = getattr(self, "session_tables", []) + [rows]
    def foreign_turn(self, surface, role, text):
        self.foreign = getattr(self, "foreign", []) + [(surface, role, text)]
    # TurnSink no-ops
    def tool_start(self, *a): ...
    def tool_result(self, *a): ...
    def ask_consent(self, *a): return "yes"
    def consent_dismissed(self, note): ...
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
                         intel_factory=kw.pop("intel_factory", lambda cfg, ws: _NoopIntel()),
                         shadow_factory=kw.pop("shadow_factory", lambda cfg, ws: None)))
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
    # Liveness: this path MUST clear busy via end_turn("") -- a stuck busy
    # flag locked the whole dock out (live 2026-07-15) and starves the idle-
    # steer poller. Empty text = no final panel, just the state reset.
    assert sink.turns == [""]


def test_stream_auth_error_renders_login_hint():
    # A real auth verdict (stream 401 that survived the forced refresh, or a
    # dead local session) must render a clean actionable message -- never a
    # raw "Error: StreamAuthError: ..." traceback string. Name-matched (not
    # imported) per repl.py's except clause, so a plain local class with the
    # right __name__ exercises the same branch a real StreamAuthError would.
    class StreamAuthError(Exception):
        pass

    class AuthDeadAgent(FakeAgent):
        async def run(self, task, sink, *, marathon=False, goal=""):
            self.tasks.append(task)
            raise StreamAuthError("stream 401")

    agent = AuthDeadAgent()
    sink, agent = _run(read_line=_lines("do it", "/exit"), agent=agent)
    assert agent.tasks == ["do it"]
    assert any("run /login" in n for n in sink.notes)
    assert not any(n.startswith("Error:") for n in sink.notes)
    assert not any(NO_CYRILLIC.search(n) for n in sink.notes)
    # Same liveness guarantee as the generic-error path: busy must clear.
    assert sink.turns == [""]


def test_run_marathon_stream_auth_error_renders_login_hint_and_clears_busy():
    # FIX7c coverage: run_marathon has its OWN copy of the same StreamAuthError
    # -> login-hint handling run_repl's _run_turn has (the `sink` vs `_sink`
    # duplicated branch) -- until now it had ZERO test coverage of its own.
    class StreamAuthError(Exception):
        pass

    class AuthDeadAgent:
        async def run(self, task, sink, *, marathon=True, goal=""):
            raise StreamAuthError("stream 401")

    class _Auth:
        async def ensure_access_token(self, cfg, force=False):
            return "tok"

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")
    sink = FakeSink()
    agent = AuthDeadAgent()
    text = asyncio.run(run_marathon(
        cfg, "default", "build a thing", sink=sink, auth=_Auth(),
        agent_factory=lambda c, tp, ws, m: agent))
    assert text == ""
    assert any("run /login" in n for n in sink.notes)
    assert not any(n.startswith("Error:") for n in sink.notes)
    assert sink.turns == [""]     # busy cleared via end_turn("") -- same liveness guarantee


def test_error_turn_marks_failed_and_notes_held_queue():
    # W1 task 6: an ERROR-terminated turn must mark the sink (so the dock's
    # drain rule holds the type-ahead queue) and, when lines are already
    # waiting, tell the user honestly that they're held rather than silently
    # sitting there. `sink.local_pending` is the SAME deque object repl.py
    # hands the dock (repl._boot: `s.local_pending = pending_queue`) -- the
    # agent mutates it here to simulate two lines queued while this turn ran.
    class RaisingAgent(FakeAgent):
        async def run(self, task, sink, *, marathon=False, goal=""):
            self.tasks.append(task)
            sink.local_pending.append("queued 1")
            sink.local_pending.append("queued 2")
            raise OSError("network down")

    agent = RaisingAgent()
    sink, agent = _run(read_line=_lines("do it", "/exit"), agent=agent)
    assert agent.tasks == ["do it"]
    assert sink.turn_failed_marks == 1                     # mark_turn_failed() fired
    # FIX6: the note must be honest about what actually works -- Enter on an
    # EMPTY input is a no-op (the old text advertised a dead gesture); ↑
    # pulls the next queued item into the input for real.
    assert any("queue held: 2" in n and "↑ pulls the next into the input" in n
              for n in sink.notes)
    assert not any(NO_CYRILLIC.search(n) for n in sink.notes)
    assert sink.turns == [""]                               # busy still clears (liveness)


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


def test_clear_command_scopes_to_the_active_slot_only():
    # Task 7 item 3: /clear must touch ONLY the ACTIVE slot's own sink --
    # same "the action lands on the right slot's sink, not the original"
    # proof style as test_new_tab_notes_on_the_new_slots_own_sink_not_the_original.
    sink, agent = _run(read_line=_lines("/new /tmp", "/clear", "/tab 0", "/exit"))
    # /clear ran while the NEW (real-sink) slot was active -- the ORIGINAL
    # FakeSink was a BACKGROUND slot at that moment and must be untouched.
    assert sink.cleared is False
    assert not any("cleared" in n.lower() for n in sink.notes)

    sink2, agent2 = _run(read_line=_lines("/new /tmp", "/tab 0", "/clear", "/exit"))
    # Reversed order: /clear now runs while the ORIGINAL slot IS active --
    # positive proof the active slot's own sink.clear() genuinely fires.
    assert sink2.cleared is True
    assert any("cleared" in n.lower() for n in sink2.notes)


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
    # Liveness: this path MUST clear busy via end_turn("") -- a stuck busy
    # flag locked the whole dock out (live 2026-07-15) and starves the idle-
    # steer poller. Empty text = no final panel, just the state reset.
    assert sink.turns == [""]


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
        sessions_client=FakeSessions(), shadow_factory=lambda cfg, ws: None,
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
        sessions_client=FakeSessions(), shadow_factory=lambda cfg, ws: None,
    ))
    assert _SpyAgent.last_intel is None
    assert sink.turns == []  # boot completed cleanly, no crash mid-boot


def test_boot_shares_local_queue_with_sink():
    # The repl hands the sink the SAME type-ahead deque tui mutates, so the
    # kernel's task_queued echo can promote a local twin into the single
    # kernel-owned row (queue-panel single-source dedup, 0.3.16).
    from collections import deque
    sink, agent = _run(read_line=_lines("/exit"))
    assert isinstance(getattr(sink, "local_pending", None), deque)


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
        sessions_client=FakeSessions(), shadow_factory=lambda cfg, ws: None,
    ))
    assert called == []                    # intel_factory never invoked
    assert _SpyAgent.last_intel is None


# ── /notify remote control (Task 8) ───────────────────────────────────────────

def test_notify_sets_remote_and_shows_state(monkeypatch):
    agent = StepAgent()

    async def fake_set(cfg, tp, sid, arg):
        assert sid == "sess-1" and arg == "tg"
        return {"enabled": True, "mirror": ["telegram"], "steer": ["telegram"]}
    monkeypatch.setattr("webbee.remote.set_remote", fake_set)

    sink, agent = _run(read_line=_lines("/notify tg", "/exit"), agent=agent)
    assert any("ON" in n for n in sink.notes)


def test_notify_without_session_prompts_to_start_a_turn():
    sink, agent = _run(read_line=_lines("/notify tg", "/exit"), agent=StepAgent(session_id=""))
    assert any("Start a coding turn first" in n for n in sink.notes)


def test_notify_bad_arg_shows_usage():
    sink, agent = _run(read_line=_lines("/notify bogus", "/exit"), agent=StepAgent())
    assert any("Usage: /notify" in n for n in sink.notes)


def test_notify_network_error_notes_cleanly(monkeypatch):
    async def fake_set(cfg, tp, sid, arg):
        raise RuntimeError("connection refused")
    monkeypatch.setattr("webbee.remote.set_remote", fake_set)

    sink, agent = _run(read_line=_lines("/notify tg", "/exit"), agent=StepAgent())
    assert any("Remote control unavailable" in n for n in sink.notes)


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
        sessions_client=FakeSessions(), shadow_factory=lambda cfg, ws: None,
    ))
    assert captured.get("root") == str(tmp_path)
    assert captured.get("root") != os.getcwd()


# ── boot replay of the durable coding thread (Task 9) ─────────────────────────

class _FakeImperalClient:
    """Stands in for imperal_mcp.client.ImperalClient -- house pattern already
    used by test_session.py/test_marathon.py for the same external class."""
    def __init__(self, cfg, token_provider):
        pass

    async def whoami(self):
        return "user-1"


def test_boot_replays_recent_thread_before_live_loop(monkeypatch):
    import imperal_mcp.client as ic
    import webbee.thread as TH

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    async def fake_fetch(cfg, token_provider, session_id):
        assert session_id == "marathon-user-1-rboot"     # _owns prefix contract
        return [
            {"role": "user", "content": "hi", "surface": "telegram"},
            {"role": "assistant", "content": "done", "surface": "terminal"},
        ]
    monkeypatch.setattr(TH, "fetch_recent_thread", fake_fetch)

    sink, agent = _run(read_line=_lines("/exit"))
    assert sink.foreign == [
        ("telegram", "user", "hi"),           # renders as "you [telegram]: hi"
        ("terminal", "assistant", "done"),
    ]
    assert any("live" in n for n in sink.notes)
    # replay happened during boot, strictly before any turn ran
    assert agent.tasks == []


def test_boot_replay_survives_thread_fetch_failure(monkeypatch):
    import imperal_mcp.client as ic
    import webbee.thread as TH

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    async def boom(cfg, token_provider, session_id):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(TH, "fetch_recent_thread", boom)

    sink, agent = _run(read_line=_lines("hello", "/exit"))
    assert getattr(sink, "foreign", []) == []
    assert agent.tasks == ["hello"]            # boot completed cleanly, agent still works


def test_boot_replay_survives_whoami_failure(monkeypatch):
    # No imperal_mcp.client.ImperalClient patched here -- whoami() hits the
    # (unreachable) fake api_url and raises. The whole replay block must be
    # swallowed, never crashing boot or delaying it past the intel/agent setup.
    sink, agent = _run(read_line=_lines("hello", "/exit"))
    assert getattr(sink, "foreign", []) == []
    assert agent.tasks == ["hello"]


def test_boot_replay_truncates_long_messages(monkeypatch):
    import imperal_mcp.client as ic
    import webbee.thread as TH

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    long_text = "x" * 1000

    async def fake_fetch(cfg, token_provider, session_id):
        return [{"role": "assistant", "content": long_text, "surface": "terminal"}]
    monkeypatch.setattr(TH, "fetch_recent_thread", fake_fetch)

    sink, agent = _run(read_line=_lines("/exit"))
    assert len(sink.foreign) == 1
    rendered = sink.foreign[0][2]
    assert len(rendered) <= 401                 # 400 chars + ellipsis
    assert rendered.endswith("…")


def test_boot_replay_skips_note_when_thread_empty(monkeypatch):
    import imperal_mcp.client as ic
    import webbee.thread as TH

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    async def fake_fetch(cfg, token_provider, session_id):
        return []
    monkeypatch.setattr(TH, "fetch_recent_thread", fake_fetch)

    sink, agent = _run(read_line=_lines("/exit"))
    assert getattr(sink, "foreign", []) == []
    assert not any("live" in n for n in sink.notes)


# ── idle-steer pickup wiring (liveness v2 §B) ─────────────────────────────────
# The poll loop itself lives in webbee.steer (unit-tested in test_steer.py);
# these cover ONLY the repl wiring: the poller task starts at boot with the
# right seams, its submit renders the remote line + runs a surface-tagged turn
# through the SAME path a typed line takes, and it is cancelled on exit.

class SurfaceAgent(FakeAgent):
    """FakeAgent that accepts the additive `surface` turn kwarg and yields to
    the event loop once per run so the boot-started poller task can interleave
    with a typed turn (the plain fallback read_line is sync)."""
    session_id = "marathon-user-1-rab12cd34ef56"

    async def run(self, task, sink, *, marathon=False, goal="", surface="",
                  steer_iid=""):
        self.tasks.append(task)
        self.runs.append({"task": task, "marathon": marathon, "goal": goal,
                          "surface": surface, "steer_iid": steer_iid})
        await asyncio.sleep(0)
        return f"answer:{task}"


def test_steer_pickup_renders_remote_line_and_runs_tagged_turn(monkeypatch):
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, is_busy, submit,
                         marathon=True, live_session_id=lambda: "", **kw):
        captured["marathon"] = marathon
        captured["live_sid"] = live_session_id()
        await submit("push the fix", "telegram", "iid-42")

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)

    agent = SurfaceAgent()
    # three yield points: "hello" lets the boot-started poller task interleave
    # (submit begins the steer turn), "again" lets the steer turn COMPLETE
    # before /exit tears the loop down (read_line itself is sync here).
    sink, agent = _run(read_line=_lines("hello", "again", "/exit"), agent=agent)
    # the remote user's line renders tagged with its origin surface...
    assert ("telegram", "user", "push the fix") in getattr(sink, "foreign", [])
    # ...and the SAME turn path ran it, threading surface + the item's dedup
    # iid into the turn kwargs (steer-iid-dedup pickup path)
    steer_runs = [r for r in agent.runs if r["task"] == "push the fix"]
    assert steer_runs and steer_runs[0]["surface"] == "telegram"
    assert steer_runs[0]["steer_iid"] == "iid-42"
    assert steer_runs[0]["marathon"] is True          # normal marathon turn
    # a picked-up turn ends like any other -- end_turn fired, dock leaves busy
    assert "answer:push the fix" in sink.turns
    # the wiring hands the poller the agent's LIVE session id seam (gateway
    # truth once a turn has run; webbee.steer derives the repo id before that)
    assert captured["live_sid"] == "marathon-user-1-rab12cd34ef56"
    assert captured["marathon"] is True


def test_steer_poller_busy_gate_reads_sink(monkeypatch):
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, is_busy, submit, **kw):
        captured["is_busy"] = is_busy

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)

    class BusySink(FakeSink):
        def is_busy(self):
            return True

    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent(),
                       sink=BusySink())
    # the poller's busy gate is the sink's live turn state (begin/end_turn)
    assert captured["is_busy"]() is True


def test_steer_poller_without_sink_busy_hook_defaults_idle(monkeypatch):
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, is_busy, submit, **kw):
        captured["is_busy"] = is_busy

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent())
    assert captured["is_busy"]() is False   # FakeSink has no is_busy -> never blocks


def test_steer_poller_once_mode_polls_coding_session(monkeypatch):
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, marathon=True, **kw):
        captured["marathon"] = marathon

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent(),
                       once=True)
    assert captured["marathon"] is False    # -> coding-{iid}-r{key} derivation


def test_steer_poller_cancelled_on_exit(monkeypatch):
    import webbee.steer as SP
    fate = {}

    async def hanging_poller(cfg, token_provider, **kw):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            fate["cancelled"] = True
            raise

    monkeypatch.setattr(SP, "poll_idle_steer", hanging_poller)
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent())
    # no leaked task: the repl cancelled the poller on exit and the loop closed
    # cleanly (asyncio.run inside _run would warn/hang otherwise)
    assert fate.get("cancelled") is True


def test_shared_client_closed_on_repl_exit(monkeypatch):
    # FIX7f coverage: the repl-lifetime keep-alive AsyncClient (Task 12) must
    # be closed when the fallback loop exits -- a leaked client keeps its
    # connection pool (and the event loop) alive past the repl's lifetime.
    import webbee.http as H
    import webbee.steer as SP

    async def noop_poller(cfg, token_provider, **kw):
        ...

    monkeypatch.setattr(SP, "poll_idle_steer", noop_poller)

    closed = {"n": 0}

    class _FakeClient:
        async def aclose(self):
            closed["n"] += 1

    monkeypatch.setattr(H, "make_client", lambda cfg: _FakeClient())

    sink, agent = _run(read_line=_lines("/exit"))
    assert closed["n"] == 1


def test_queue_command_in_fallback_loop_reports_empty_and_never_hits_agent():
    # /queue // /queue clear are pure display: the fallback loop (no dock) has
    # an always-empty queue — honest messages, and the agent is never invoked.
    sink, agent = _run(read_line=_lines("/queue", "/queue clear", "/exit"))
    assert any("empty" in n.lower() for n in sink.notes)
    assert any("already empty" in n.lower() for n in sink.notes)
    assert agent.tasks == []


# ── remote mode adoption wiring (autopilot safe-asymmetry, 0.3.14) ────────────
# webbee.steer hands requested_mode to the repl's on_mode seam. Repl policy
# (Valentin-chosen): a downgrade/lateral (→ default/plan) applies INSTANTLY
# with an audited note; the upgrade → autopilot NEVER applies silently — a
# terminal-local y/n confirm (sink.ask_yes_no) must approve it, and anything
# short of an explicit local yes keeps the current mode with an audited note.

def _spy_on_mode(monkeypatch, *calls, settle_ticks=4):
    """Capture the repl's injected on_mode seam and feed it `calls`; each call
    is followed by a few loop ticks so a spawned confirm task can finish."""
    import webbee.steer as SP

    async def spy_poller(cfg, token_provider, *, on_mode=None, **kw):
        for mode, surface in calls:
            on_mode(mode, surface)
            for _ in range(settle_ticks):
                await asyncio.sleep(0)

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)


def test_remote_downgrade_applies_instantly_with_note(monkeypatch):
    _spy_on_mode(monkeypatch, ("plan", "telegram"))
    agent = SurfaceAgent()
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=agent)
    assert agent.mode == "plan"                       # applied, no prompt needed
    assert any(n == "mode → plan [telegram]" for n in sink.notes)


def test_remote_autopilot_applies_only_on_local_yes(monkeypatch):
    _spy_on_mode(monkeypatch, ("autopilot", "telegram"))

    class ConfirmSink(FakeSink):
        def __init__(self):
            super().__init__()
            self.questions = []
        async def ask_yes_no(self, question, timeout=60.0):
            self.questions.append(question)
            return True

    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent(),
                       sink=ConfirmSink())
    assert agent.mode == "autopilot"                  # flipped only AFTER the local yes
    assert sink.questions and "autopilot" in sink.questions[0]
    assert "telegram" in sink.questions[0] and "allow?" in sink.questions[0]
    assert any("approved at this terminal" in n for n in sink.notes)


def test_remote_autopilot_declined_or_unconfirmable_keeps_mode(monkeypatch):
    _spy_on_mode(monkeypatch, ("autopilot", "telegram"))

    class DeclineSink(FakeSink):
        async def ask_yes_no(self, question, timeout=60.0):
            return False                              # n / timeout / no reply

    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent(),
                       sink=DeclineSink())
    assert agent.mode == "default"                    # unchanged
    assert any("declined" in n and "default" in n for n in sink.notes)

    # No confirm affordance at all (minimal sink) -> fail-safe, audited.
    _spy_on_mode(monkeypatch, ("autopilot", "telegram"))
    sink2, agent2 = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent())
    assert agent2.mode == "default"
    assert any("not applied" in n for n in sink2.notes)


def test_remote_mode_unknown_or_noop_is_dropped(monkeypatch):
    _spy_on_mode(monkeypatch, ("turbo", "telegram"), ("default", "telegram"))
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent())
    assert agent.mode == "default"                    # unknown dropped; no-op silent
    assert not any(n.startswith("mode →") for n in sink.notes)


def test_poller_busy_ignores_stale_busy_flag_when_task_dead():
    """sink.is_busy() True but turn_ref['task'] is a DONE future ⇒
    _gate_busy() False (the 0.3.8-class stuck-flag no longer starves the
    idle-steer poller)."""
    from webbee.repl import _gate_busy

    class BusySink(FakeSink):
        def is_busy(self): return True

    class _DoneTask:
        def done(self): return True

    assert _gate_busy(BusySink(), {"task": _DoneTask()}) is False


def test_poller_busy_true_while_task_alive():
    """sink busy + a pending future ⇒ True (unchanged)."""
    from webbee.repl import _gate_busy

    class BusySink(FakeSink):
        def is_busy(self): return True

    class _PendingTask:
        def done(self): return False

    assert _gate_busy(BusySink(), {"task": _PendingTask()}) is True


def test_steer_poller_busy_gate_also_holds_while_local_prompt_armed(monkeypatch):
    # The autopilot confirm arms the same pinned-input future a consent uses;
    # the poller must hold off then (a steer turn under an armed prompt could
    # double-prompt the input).
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, is_busy, **kw):
        captured["is_busy"] = is_busy

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)

    class ArmedSink(FakeSink):
        def is_busy(self): return False
        def consent_pending(self): return True

    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent(),
                       sink=ArmedSink())
    assert captured["is_busy"]() is True


# ── 0.3.15: mid-turn inject wiring ────────────────────────────────────────────
# _inject_via_gateway is the dock's Enter-while-busy gateway leg (module-level
# so it's driven directly): it POSTs into the agent's LIVE session, echoes the
# sent line on ok, and returns False on EVERY failure path so the dock falls
# back to the local type-ahead queue. The drained fallback row (tui.QueuedLine)
# then threads its minted steer_iid into the normal turn path — the kernel's
# dedup ring drops the twin if the inject landed after all.

def test_inject_via_gateway_posts_and_echoes_on_ok(monkeypatch):
    import webbee.thread as TH
    from webbee.config import Config
    from webbee.repl import _inject_via_gateway
    seen = {}

    async def fake_inject(cfg, token_provider, session_id, text, steer_iid):
        seen.update(sid=session_id, text=text, iid=steer_iid)
        return True

    monkeypatch.setattr(TH, "inject_to_session", fake_inject)
    sink, agent = FakeSink(), SurfaceAgent()

    async def _tp(): return "tok"
    ok = asyncio.run(_inject_via_gateway(Config(api_url="http://x", panel_url="http://p"),
                                         _tp, agent, sink, "fly this", "iid-9"))
    assert ok is True
    assert seen == {"sid": "marathon-user-1-rab12cd34ef56",
                    "text": "fly this", "iid": "iid-9"}
    assert sink.echoed == ["fly this"]        # the transcript records it as sent


def test_inject_via_gateway_false_without_live_session_or_on_error(monkeypatch):
    import webbee.thread as TH
    from webbee.config import Config
    from webbee.repl import _inject_via_gateway
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def _tp(): return "tok"

    # no live session yet → False, and the gateway is never called
    called = []

    async def spy_inject(*a, **kw):
        called.append(a)
        return True

    monkeypatch.setattr(TH, "inject_to_session", spy_inject)
    sink = FakeSink()
    assert asyncio.run(_inject_via_gateway(cfg, _tp, FakeAgent(), sink, "x", "i")) is False
    assert called == []

    # a network/auth error → False (fail-soft, the local queue takes over)
    async def boom(*a, **kw):
        raise RuntimeError("offline")

    monkeypatch.setattr(TH, "inject_to_session", boom)
    assert asyncio.run(_inject_via_gateway(cfg, _tp, SurfaceAgent(), sink, "x", "i")) is False
    assert getattr(sink, "echoed", []) == []  # nothing echoed on any failure


def test_drained_queued_line_threads_its_steer_iid_into_the_turn():
    # A failed-inject fallback row drains at turn end through the SAME _handle
    # path a typed line takes — its QueuedLine iid must ride into the turn
    # POST (kernel dedup ring), while a plain typed line threads none.
    from webbee.tui import QueuedLine
    agent = SurfaceAgent()
    sink, agent = _run(read_line=_lines(QueuedLine("run the fallback", "iid-77"),
                                        "plain typed", "/exit"), agent=agent)
    runs = {r["task"]: r for r in agent.runs}
    assert runs["run the fallback"]["steer_iid"] == "iid-77"
    assert runs["plain typed"]["steer_iid"] == ""
    assert "run the fallback" in getattr(sink, "echoed", [])   # normal ❯ echo path


# ── W4a Task 2: repl boot split -- slot factory + process/workspace/slot ─────
# `_make_session_slot`/`_finish_slot` build the atomic {agent, sink, pane}
# triple (wiring map §6); `_slot_ctx`/`_live_session_id` are the pure,
# module-level extractions the run_repl closures (_ctx, the steer poller's
# live_session_id) now read from -- driven directly here without needing to
# run the whole REPL loop.

async def _noop_token_provider():
    return "tok"


def _mk_cfg():
    from webbee.config import Config
    return Config(api_url="http://x", panel_url="http://p")


def test_make_session_slot_builds_coupled_atomic_triple():
    # The sink must point at THIS slot's own pane/console (wiring map §6 --
    # a sink must never point at another slot's pane), and its local queue
    # must be THIS slot's own pending deque, not a shared/global one.
    agent = FakeAgent()

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, "/tmp", "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: agent,
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    slot = asyncio.run(_drive())
    assert slot.kind == "session"
    assert slot.sink.console is slot.pane.console
    assert slot.sink.local_pending is slot.pending
    assert slot.agent is agent


def test_make_session_slot_shares_resources_bundle_on_same_workspace():
    # Two slots opened on the SAME repo root must share ONE intel instance
    # (map §6: same workspace -> same intel/shadow/git_branch bundle) --
    # the intel_factory must fire exactly once, not once per slot.
    resources = WorkspaceResources()
    built = []

    def intel_factory(cfg, ws):
        svc = _NoopIntel()
        built.append(svc)
        return svc

    cfg = _mk_cfg()
    workspace = os.getcwd()

    async def _drive():
        s1 = await _make_session_slot(
            cfg, _noop_token_provider, workspace, "default",
            resources=resources, shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=intel_factory, shadow_factory=lambda cfg, ws: None,
            first=False)
        s2 = await _make_session_slot(
            cfg, _noop_token_provider, workspace, "default",
            resources=resources, shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=intel_factory, shadow_factory=lambda cfg, ws: None,
            first=False)
        return s1, s2

    s1, s2 = asyncio.run(_drive())
    assert len(built) == 1                              # ONE boot, not two
    bundle = resources.get(workspace)
    assert bundle["intel"] is built[0]
    # both slots' default agent_factory (if used) would have captured the
    # SAME bundle -- here the custom agent_factory ignores it, but the
    # sharing itself (the cache) is the thing under test.
    assert s1.git_branch == s2.git_branch


def test_make_session_slot_first_false_skips_replay(monkeypatch):
    from webbee import boot
    calls = []

    async def fake_replay(cfg, tp, sink):
        calls.append(sink)

    monkeypatch.setattr(boot, "replay_thread", fake_replay)

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    asyncio.run(_drive())
    assert calls == []            # first=False -> replay_thread never awaited


def test_make_session_slot_first_true_runs_replay(monkeypatch):
    from webbee import boot
    calls = []

    async def fake_replay(cfg, tp, sink):
        calls.append(sink)

    monkeypatch.setattr(boot, "replay_thread", fake_replay)

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=True)

    slot = asyncio.run(_drive())
    assert calls == [slot.sink]   # first=True -> replay runs into THIS slot's sink


def test_slot_ctx_reads_active_slot_and_flips_on_switch():
    mgr = SlotManager()
    home = SessionSlot(kind="home", workspace="/ws-home", label="Home",
                       pane=object(), sink=None, agent=None)
    mgr.add(home)

    sink = FakeSink()
    sink.session_tokens, sink.session_credits = 42, 7
    session = SessionSlot(kind="session", workspace="/ws-a", label="a",
                          pane=object(), sink=sink, agent=FakeAgent(),
                          mode="plan", git_branch="feature-x")
    session.pending.append("queued line")
    mgr.add(session)
    mgr.active_idx = 1

    ctx = _slot_ctx(mgr.active(), logged_in=True)
    assert (ctx.mode, ctx.workspace, ctx.git_branch) == ("plan", "/ws-a", "feature-x")
    assert ctx.queued == ("queued line",)
    assert (ctx.session_tokens, ctx.session_credits) == (42, 7)

    mgr.active_idx = 0   # switch to Home -- agentless/sinkless, must not crash
    ctx_home = _slot_ctx(mgr.active(), logged_in=True)
    assert ctx_home.workspace == "/ws-home"
    assert ctx_home.git_branch == "-"           # SessionSlot's own default
    assert ctx_home.session_tokens == 0         # sink is None -> getattr default


def test_slot_ctx_flips_between_two_session_slots_with_distinct_state():
    # Task 7 item 3: /status, /cost and /queue all render straight from
    # `_ctx()` -> `_slot_ctx(slots.active(), ...)` -- two REAL session slots
    # (not Home vs. session) with their own mode/queue/spend prove the
    # snapshot genuinely flips with the active slot, not just "Home reads as
    # empty" (already covered above).
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace="/ws-home", label="Home",
                        pane=object(), sink=None, agent=None))
    sink_a = FakeSink()
    sink_a.session_tokens, sink_a.session_credits = 100, 10
    slot_a = SessionSlot(kind="session", workspace="/ws-a", label="a",
                         pane=object(), sink=sink_a, agent=FakeAgent(),
                         mode="plan", git_branch="feature-a")
    slot_a.pending.append("a's queued line")
    mgr.add(slot_a)
    sink_b = FakeSink()
    sink_b.session_tokens, sink_b.session_credits = 5, 1
    slot_b = SessionSlot(kind="session", workspace="/ws-b", label="b",
                         pane=object(), sink=sink_b, agent=FakeAgent(),
                         mode="autopilot", git_branch="feature-b")
    slot_b.pending.extend(["b1", "b2"])
    mgr.add(slot_b)

    mgr.active_idx = 1
    ctx_a = _slot_ctx(mgr.active(), logged_in=True)
    assert (ctx_a.mode, ctx_a.workspace, ctx_a.git_branch) == ("plan", "/ws-a", "feature-a")
    assert ctx_a.queued == ("a's queued line",)
    assert (ctx_a.session_tokens, ctx_a.session_credits) == (100, 10)

    mgr.active_idx = 2
    ctx_b = _slot_ctx(mgr.active(), logged_in=True)
    assert (ctx_b.mode, ctx_b.workspace, ctx_b.git_branch) == ("autopilot", "/ws-b", "feature-b")
    assert ctx_b.queued == ("b1", "b2")
    assert (ctx_b.session_tokens, ctx_b.session_credits) == (5, 1)


def test_live_session_id_survives_agent_none_on_home():
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace="/ws", label="Home",
                        pane=object(), sink=None, agent=None))
    mgr.add(SessionSlot(kind="session", workspace="/ws", label="a", pane=object(),
                        sink=FakeSink(), agent=StepAgent(session_id="sess-123")))

    mgr.active_idx = 0
    assert _live_session_id(mgr) == ""           # Home active -> no crash, empty id

    mgr.active_idx = 1
    assert _live_session_id(mgr) == "sess-123"


def test_watcher_task_cancelled_on_repl_exit(monkeypatch):
    # W4a: the per-workspace resources bundle's watcher_task lives in
    # WorkspaceResources now, not a repl-level nonlocal -- _cancel_background
    # must still reach it and cancel it on exit (map §5).
    fate = {}

    async def hanging_watch(root, on_change):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            fate["cancelled"] = True
            raise

    from webbee.intel import watch
    monkeypatch.setattr(watch, "watch_workspace", hanging_watch)

    class _RootedIntel:
        def __init__(self):
            self.root = os.getcwd()
        def build(self): ...
        def apply_changes(self, paths): ...

    sink, agent = _run(read_line=_lines("/exit"), intel_factory=lambda cfg, ws: _RootedIntel())
    assert fate.get("cancelled") is True


# ── W4a Task 5: tab keys + commands + lifecycle — repl-side wiring ──────────


class _FakeTask:
    def __init__(self, done=False):
        self._done = done
        self.cancelled = False
    def done(self):
        return self._done
    def cancel(self):
        self.cancelled = True


def test_cancel_slot_cancels_the_running_turn_task_and_bg_tasks():
    slot = SessionSlot(kind="session", workspace=".", label="t",
                       pane=object(), sink=None, agent=None)
    live_turn = _FakeTask()
    slot.turn["task"] = live_turn
    live_bg, done_bg = _FakeTask(), _FakeTask(done=True)
    slot.bg_tasks = [live_bg, done_bg, None]

    _cancel_slot(slot)

    assert live_turn.cancelled is True
    assert live_bg.cancelled is True
    assert done_bg.cancelled is False        # already done -- never double-cancelled


def test_cancel_slot_survives_no_turn_task_and_no_bg_tasks():
    slot = SessionSlot(kind="session", workspace=".", label="t",
                       pane=object(), sink=None, agent=None)
    _cancel_slot(slot)                       # must not raise -- turn["task"] is None


def test_new_tab_command_opens_a_second_slot_and_switches_to_it():
    # Fallback (non-dock) path: ui_hooks stays {} so /new's switch falls back
    # to slots.switch directly -- no history swap needed with no dock, but
    # the slot itself must exist and become active, and /tabs (after
    # switching back) must show BOTH tabs with the right glyphs/labels.
    sink, agent = _run(read_line=_lines("/new /tmp", "/tab 0", "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert "●0" in listing and "○1" in listing
    assert "tmp" in listing


def test_new_tab_notes_on_the_new_slots_own_sink_not_the_original():
    sink, agent = _run(read_line=_lines("/new /tmp", "/exit"))
    # the "tab N opened" note lands on the NEW slot's own (real) sink, so the
    # original FakeSink returned by _run never sees it.
    assert not any("opened" in n for n in sink.notes)


def test_tab_switch_bad_index_notes_helpfully():
    sink, agent = _run(read_line=_lines("/tab 5", "/exit"))
    assert any("No such tab" in n for n in sink.notes)


def test_tab_switch_valid_index_switches_active_slot():
    sink, agent = _run(read_line=_lines("/new /tmp", "/tab 0", "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert listing.startswith("Open tabs:")
    assert "●0" in listing                    # back on the original slot


def test_close_command_on_the_only_slot_notes_nothing_to_close():
    # The fallback loop's single slot sits at index 0 -- unconditionally
    # guarded by SlotManager.close (the real Home-at-0 invariant), same as
    # production's Home tab.
    sink, agent = _run(read_line=_lines("/close", "/exit"))
    assert any("Nothing to close" in n for n in sink.notes)


def test_close_command_closes_the_new_tab_and_notes_the_survivor():
    sink, agent = _run(read_line=_lines("/new /tmp", "/close", "/tabs", "/exit"))
    assert any("server-side" in n and "/new" in n for n in sink.notes)
    listing = sink.notes[-1]
    assert "●0" in listing and "1" not in listing.replace("/tmp", "")


def test_tabs_list_note_contains_one_line_per_tab_with_glyphs():
    # /new switches active to the new slot -- switch back to 0 first so the
    # /tabs note lands on the ORIGINAL (inspectable) FakeSink.
    sink, agent = _run(read_line=_lines("/new /tmp", "/tab 0", "/tabs", "/exit"))
    listing = sink.notes[-1]
    lines = listing.split("\n")
    assert lines[0] == "Open tabs:"
    assert len(lines) == 3                    # header + 2 tabs
    assert lines[1].startswith("●0")           # back on the original slot
    assert lines[2].startswith("○1")


# ── W4a Task 7: multi-tab edges -- steer targeting, exit dump, cancellation ──
# _steer_target/_exit_dump/_cancel_all_background are module-level and pure
# (same DI-testing philosophy as _gate_busy/_live_session_id/_cancel_slot) so
# each is driven directly here without needing a live dock or the fallback
# loop's single-slot world (which can never grow a Home slot to exercise the
# Home-routing/none-target branches at all).

def test_steer_target_active_session_returns_itself():
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=object(), sink=None, agent=None))
    session = SessionSlot(kind="session", workspace=".", label="a",
                          pane=object(), sink=FakeSink(), agent=None)
    mgr.add(session)
    mgr.active_idx = 1
    assert _steer_target(mgr) is session


def test_steer_target_home_active_routes_to_the_first_session_slot():
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=object(), sink=None, agent=None))
    first = SessionSlot(kind="session", workspace=".", label="a",
                        pane=object(), sink=FakeSink(), agent=None)
    second = SessionSlot(kind="session", workspace=".", label="b",
                         pane=object(), sink=FakeSink(), agent=None)
    mgr.add(first)
    mgr.add(second)
    mgr.active_idx = 0                          # Home is the ACTIVE tab
    assert _steer_target(mgr) is first          # lowest-index session, never second


def test_steer_target_none_when_no_session_slot_exists():
    # Every tab closed down to bare Home -- both _poller_busy and
    # _steer_submit must treat this as "nothing to submit into", not crash
    # reaching for a None sink.
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=object(), sink=None, agent=None))
    mgr.active_idx = 0
    assert _steer_target(mgr) is None


class _FakePane:
    """Minimal `.dump()` double for `_exit_dump` -- doesn't need a real
    OutputPane (or prompt_toolkit) at all, unlike an end-to-end dock test."""
    def __init__(self, text):
        self._text = text
    def dump(self):
        return self._text


def test_exit_dump_single_session_slot_has_no_separator():
    # Pinned: today's single-tab output must stay byte-identical to a bare
    # `pane.dump()` -- no separator text appears at all with just one slot.
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=_FakePane("HOME"), sink=None, agent=None))
    mgr.add(SessionSlot(kind="session", workspace=".", label="a",
                        pane=_FakePane("transcript-a"), sink=None, agent=None))
    assert _exit_dump(mgr) == "transcript-a"


def test_exit_dump_multi_session_slots_get_separators_and_skip_home():
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=_FakePane("HOME-STUFF"), sink=None, agent=None))
    mgr.add(SessionSlot(kind="session", workspace=".", label="a",
                        pane=_FakePane("A-TEXT"), sink=None, agent=None))
    mgr.add(SessionSlot(kind="session", workspace=".", label="b",
                        pane=_FakePane("B-TEXT"), sink=None, agent=None))
    out = _exit_dump(mgr)
    assert "HOME-STUFF" not in out                          # Home never dumped
    # a separator lands BETWEEN panes only -- none before the first, and its
    # index is the slot's OWN SlotManager index (matches /tab N + the tab
    # bar's own numbering), not a 1-based session ordinal.
    assert out == "A-TEXT── tab 2: b ──\nB-TEXT"


def test_exit_dump_no_session_slots_is_empty():
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=_FakePane("HOME"), sink=None, agent=None))
    assert _exit_dump(mgr) == ""


def test_cancel_all_background_sweeps_every_slots_bg_tasks_and_watchers(tmp_path):
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=object(), sink=None, agent=None))
    slot_a = SessionSlot(kind="session", workspace=".", label="a",
                         pane=object(), sink=None, agent=None)
    live_a, done_a = _FakeTask(), _FakeTask(done=True)
    slot_a.bg_tasks = [live_a, done_a, None]
    slot_b = SessionSlot(kind="session", workspace=".", label="b",
                         pane=object(), sink=None, agent=None)
    live_b = _FakeTask()
    slot_b.bg_tasks = [live_b]
    mgr.add(slot_a)
    mgr.add(slot_b)

    resources = WorkspaceResources()
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir(); root_b.mkdir()
    live_watcher, done_watcher = _FakeTask(), _FakeTask(done=True)
    resources.put(str(root_a), {"watcher_task": live_watcher})
    resources.put(str(root_b), {"watcher_task": done_watcher})

    steer = _FakeTask()
    _cancel_all_background(steer, mgr, resources)

    assert steer.cancelled is True
    assert live_a.cancelled is True
    assert done_a.cancelled is False        # already done -- never double-cancelled
    assert live_b.cancelled is True
    assert live_watcher.cancelled is True
    assert done_watcher.cancelled is False  # via the PUBLIC bundles() accessor, same guard


def test_cancel_all_background_survives_no_steer_task_and_empty_state():
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace=".", label="Home",
                        pane=object(), sink=None, agent=None))
    _cancel_all_background(None, mgr, WorkspaceResources())   # must not raise
