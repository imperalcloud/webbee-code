import asyncio
import os
import re

from webbee.account import Account
from webbee.repl import (_cancel_all_background, _cancel_slot, _exit_dump,
                         _finish_slot, _isolate_workspace, _make_session_slot,
                         _slot_ctx, run_marathon, run_repl,
                         set_slot_mode)
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


# ── set_slot_mode: the ONE place a slot's mode is ever assigned (T6.1) ───────

def test_set_slot_mode_updates_slot_and_agent(monkeypatch):
    import webbee.mode_store as MS
    monkeypatch.setattr(MS, "save_mode", lambda ws, mode: None)   # isolate: unit test, no disk
    agent = FakeAgent()
    slot = SessionSlot(kind="session", workspace="/ws-a", label="a",
                       pane=object(), sink=FakeSink(), agent=agent, mode="default")
    set_slot_mode(slot, "plan")
    assert slot.mode == "plan"
    assert agent.mode == "plan"


def test_set_slot_mode_never_crashes_on_agentless_home_slot(monkeypatch):
    import webbee.mode_store as MS
    monkeypatch.setattr(MS, "save_mode", lambda ws, mode: None)
    home = SessionSlot(kind="home", workspace="/ws-home", label="Home",
                       pane=object(), sink=None, agent=None)
    set_slot_mode(home, "plan")   # must not raise despite agent is None
    assert home.mode == "plan"


def test_set_slot_mode_persists_for_this_slots_workspace(monkeypatch):
    import webbee.mode_store as MS
    calls = []
    monkeypatch.setattr(MS, "save_mode", lambda ws, mode: calls.append((ws, mode)))
    slot = SessionSlot(kind="session", workspace="/ws-a", label="a",
                       pane=object(), sink=FakeSink(), agent=FakeAgent(), mode="default")
    set_slot_mode(slot, "autopilot")
    assert calls == [("/ws-a", "autopilot")]   # save_mode itself owns the never-persist-autopilot rule


def test_three_mode_mutation_sites_all_route_through_set_slot_mode():
    # Grep-based coverage (acceptable per the task): set_slot_mode replaces
    # the three inline mutation sites -- Shift-Tab _cycle, the /mode command
    # action, and the remote _on_mode flip (including _confirm_autopilot's
    # approval branch). _cycle itself is a dock-only closure with no
    # standalone entry point to drive behaviorally without prompt_toolkit,
    # so this asserts the wiring statically; the /mode-action and remote-flip
    # sites additionally get full behavior tests elsewhere in this file.
    # W4b T5: _on_mode/_confirm_autopilot moved to module level (one poller
    # per session slot now, each bound to its own explicit slot -- no more
    # shared first_session_slot nonlocal to close over), so their source is
    # inspected directly rather than sliced out of run_repl's own body.
    import inspect

    import webbee.repl as R
    src = inspect.getsource(R.run_repl)
    assert "def _cycle" in src
    cycle_body = src.split("def _cycle")[1].split("async def _handle")[0]
    assert "set_slot_mode(slot, next_mode(slot.mode))" in cycle_body
    assert "slot.mode = next_mode" not in cycle_body   # old two-line mutation is gone

    mode_action = src.split('res.action == "mode" and res.new_mode:')[1][:300]
    assert "set_slot_mode(slot, res.new_mode)" in mode_action

    on_mode_body = inspect.getsource(R._on_mode)
    assert "set_slot_mode(slot, mode)" in on_mode_body

    confirm_body = inspect.getsource(R._confirm_autopilot)
    assert 'set_slot_mode(slot, "autopilot")' in confirm_body


def test_mode_command_persists_mode_for_this_repo():
    import webbee.mode_store as mode_store
    sink, agent = _run(read_line=_lines("/mode plan", "/exit"))
    assert agent.mode == "plan"
    assert mode_store.load_mode(os.getcwd()) == "plan"


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


# ── boot reattach notice (T6.3, coding-remote flow perfection) ───────────────
# _note_reattach (called from _finish_slot, first=True only, right after
# replay) fetches the user's own active-session listing and renders
# webbee.active_sessions.boot_reattach_notice's verdict. Repo identity is
# mocked here (webbee.repo.compute_repo_key/find_repo_root) so these tests
# never shell out to git; the pure decision logic itself is covered in
# test_active_sessions.py.

def _patch_repo_key(monkeypatch, key="abc123"):
    import webbee.repo as R
    monkeypatch.setattr(R, "find_repo_root", lambda start: start)
    monkeypatch.setattr(R, "compute_repo_key", lambda root: key)
    return key


def test_boot_notes_reattach_when_this_repo_has_a_running_session(monkeypatch):
    key = _patch_repo_key(monkeypatch)
    import webbee.active_sessions as AS

    async def fake_fetch(cfg, tp, client=None):
        return [{"session_id": f"marathon-user-1-r{key}"}]
    monkeypatch.setattr(AS, "fetch_active_sessions", fake_fetch)

    sink, agent = _run(read_line=_lines("/exit"))
    assert any("reattached" in n and "history" in n for n in sink.notes)


def test_boot_notes_pending_approval_when_this_repos_session_is_parked(monkeypatch):
    key = _patch_repo_key(monkeypatch)
    import webbee.active_sessions as AS

    async def fake_fetch(cfg, tp, client=None):
        return [{"session_id": f"marathon-user-1-r{key}", "pending_consent": {"tool": "bash"}}]
    monkeypatch.setattr(AS, "fetch_active_sessions", fake_fetch)

    sink, agent = _run(read_line=_lines("/exit"))
    assert any("reattached" in n for n in sink.notes)
    assert any("approval" in n and "panel" in n for n in sink.notes)


def test_boot_notes_pointer_when_another_repo_has_a_parked_session(monkeypatch):
    _patch_repo_key(monkeypatch, key="abc123")
    import webbee.active_sessions as AS

    async def fake_fetch(cfg, tp, client=None):
        return [{"session_id": "marathon-user-1-rzzz999", "pending_consent": {"tool": "bash"}}]
    monkeypatch.setattr(AS, "fetch_active_sessions", fake_fetch)

    sink, agent = _run(read_line=_lines("/exit"))
    assert any("parked session waiting for approval in another repo" in n for n in sink.notes)
    assert not any("reattached" in n for n in sink.notes)


def test_boot_silent_when_no_active_sessions_anywhere(monkeypatch):
    _patch_repo_key(monkeypatch)
    import webbee.active_sessions as AS

    async def fake_fetch(cfg, tp, client=None):
        return []
    monkeypatch.setattr(AS, "fetch_active_sessions", fake_fetch)

    sink, agent = _run(read_line=_lines("/exit"))
    assert not any("reattached" in n or "parked session" in n for n in sink.notes)


def test_boot_reattach_survives_repo_key_failure(monkeypatch):
    import webbee.repo as R

    def boom(root):
        raise OSError("no git binary")
    monkeypatch.setattr(R, "find_repo_root", lambda start: start)
    monkeypatch.setattr(R, "compute_repo_key", boom)

    import webbee.active_sessions as AS
    called = []

    async def fake_fetch(cfg, tp, client=None):
        called.append(1)
        return [{"session_id": "marathon-user-1-rabc123"}]
    monkeypatch.setattr(AS, "fetch_active_sessions", fake_fetch)

    sink, agent = _run(read_line=_lines("hello", "/exit"))
    assert agent.tasks == ["hello"]           # boot completed cleanly, agent still works
    assert not any("reattached" in n for n in sink.notes)


def test_boot_reattach_survives_fetch_failure(monkeypatch):
    _patch_repo_key(monkeypatch)
    import webbee.active_sessions as AS

    async def boom(cfg, tp, client=None):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(AS, "fetch_active_sessions", boom)

    sink, agent = _run(read_line=_lines("hello", "/exit"))
    assert agent.tasks == ["hello"]
    assert not any("reattached" in n for n in sink.notes)


def test_boot_reattach_only_fires_for_first_session_slot(monkeypatch):
    _patch_repo_key(monkeypatch)
    import webbee.active_sessions as AS
    calls = []

    async def fake_fetch(cfg, tp, client=None):
        calls.append(1)
        return [{"session_id": "marathon-user-1-rabc123"}]
    monkeypatch.setattr(AS, "fetch_active_sessions", fake_fetch)

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    asyncio.run(_drive())
    assert calls == []            # first=False -> fetch_active_sessions never awaited


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


class AttachAgent(SurfaceAgent):
    """SurfaceAgent + attach() -- attach-on-poll's own turn primitive (NO
    start POST, see webbee.session.AgentSession.attach)."""

    def __init__(self):
        super().__init__()
        self.attach_calls = []

    async def attach(self, sink, *, task_id, start_id, marathon=True):
        self.attach_calls.append((task_id, start_id, marathon))
        await asyncio.sleep(0)
        return f"attached:{task_id}"


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


def test_steer_poller_gets_a_mode_getter_reading_the_first_session_slots_live_mode(monkeypatch):
    # T6.2: _spawn_steer threads a mode_getter bound to the FIRST session
    # slot (never blindly `slots.active()`) into poll_idle_steer -- read
    # fresh, so a mode change made after boot (here: /mode plan) is already
    # visible the next time the poller calls it.
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, mode_getter=None, **kw):
        captured["mode_getter"] = mode_getter

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)
    # A real task line ("hello") is needed so SurfaceAgent.run's internal
    # `await asyncio.sleep(0)` actually yields to the event loop at least
    # once -- the plain fallback read_line loop is otherwise fully sync
    # (a slash command alone never cedes control, so the scheduled poller
    # task would never get to run its body before /exit cancels it).
    sink, agent = _run(read_line=_lines("/mode plan", "hello", "/exit"), agent=SurfaceAgent())
    assert captured["mode_getter"] is not None
    assert captured["mode_getter"]() == "plan"


def test_steer_poller_gets_a_label_getter_reading_the_slots_live_label(monkeypatch):
    # W4c T3: `_spawn_slot_poller` threads a label_getter bound to THIS
    # slot -- read fresh, so an auto-label (or /rename) made after boot is
    # already visible the next time the poller calls it.
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, label_getter=None, **kw):
        captured["label_getter"] = label_getter

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)
    sink, agent = _run(read_line=_lines("fix the login bug", "/exit"), agent=SurfaceAgent())
    assert captured["label_getter"] is not None
    assert captured["label_getter"]() == "fix the login bug"


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


def test_spawn_slot_poller_wires_attach_turn_to_slot_agent_attach(monkeypatch):
    # Attach-on-poll: the poller's attach_turn seam must drive THIS slot's
    # own agent.attach() through the normal begin_turn/end_turn turn shape --
    # same discipline _steer_submit_on gets for a normal remote item.
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, attach_turn=None, **kw):
        captured["attach_turn"] = attach_turn

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)

    agent = AttachAgent()
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=agent)

    assert captured.get("attach_turn") is not None
    asyncio.run(captured["attach_turn"]({"task_id": "t1", "last_id": "5-0", "kind": "tool"}))
    assert agent.attach_calls == [("t1", "5-0", True)]  # marathon=not once -> True (default mode)
    assert "attached:t1" in sink.turns              # ended through the normal end_turn path
    assert any("attaching" in n for n in sink.notes)  # the honest attach note fired


def test_attach_turn_threads_marathon_flag_matching_once_mode(monkeypatch):
    # A --once slot's poller derives the "coding-"-prefixed session id
    # (marathon=not once); attach() must derive the SAME prefix or it
    # resumes a DIFFERENT (wrong) session than the one whose `attach`
    # field this actually is.
    import webbee.steer as SP
    captured = {}

    async def spy_poller(cfg, token_provider, *, workspace, attach_turn=None, **kw):
        captured["attach_turn"] = attach_turn

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)

    agent = AttachAgent()
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=agent, once=True)

    asyncio.run(captured["attach_turn"]({"task_id": "t1", "last_id": "5-0", "kind": "tool"}))
    assert agent.attach_calls == [("t1", "5-0", False)]  # once=True -> marathon=False


# ── W4b T5 item 3: ONE poller per SESSION slot, not one process-wide poller ──

def test_two_session_slots_each_get_their_own_idle_steer_poller(monkeypatch):
    import webbee.steer as SP
    calls = []

    async def spy_poller(cfg, token_provider, *, workspace, is_busy, submit,
                         marathon=True, live_session_id=lambda: "", mode_getter=None,
                         **kw):
        calls.append({"workspace": workspace, "mode_getter": mode_getter})

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)

    # "hello" (a real turn, SurfaceAgent's own `await asyncio.sleep(0)`) gives
    # the just-scheduled SECOND poller task at least one event-loop tick to
    # actually start running before "/exit" tears it down -- `ensure_future`
    # only SCHEDULES it, and /exit right on its heels (no intervening await)
    # would cancel it before its body ever reached `calls.append(...)`.
    sink, agent = _run(read_line=_lines("/new /tmp", "hello", "/exit"), agent=SurfaceAgent())

    # tab-1 (this process's cwd) + the "/new /tmp" tab -- TWO pollers, never
    # one shared process-wide poller chasing whichever tab is active.
    assert len(calls) == 2
    ws0, ws1 = calls[0]["workspace"], calls[1]["workspace"]
    assert ws0 != ws1
    assert os.path.abspath("/tmp") in (ws1, ws0)
    # each mode_getter reads ITS OWN slot's mode -- neither is bound to a
    # shared first_session_slot.
    assert calls[0]["mode_getter"]() == "default"
    assert calls[1]["mode_getter"]() == "default"


def test_two_session_slots_each_pollers_submit_lands_only_in_its_own_slot(monkeypatch):
    import webbee.steer as SP
    calls = []

    async def spy_poller(cfg, token_provider, *, workspace, submit, **kw):
        calls.append(submit)

    monkeypatch.setattr(SP, "poll_idle_steer", spy_poller)

    agents = []

    def agent_factory(cfg, tp, ws, mode):
        a = SurfaceAgent()
        agents.append(a)
        return a

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")
    asyncio.run(run_repl(
        cfg, "default", sink=FakeSink(), read_line=_lines("/new /tmp", "hello", "/exit"),
        agent_factory=agent_factory, auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
        sessions_client=FakeSessions(), intel_factory=lambda cfg, ws: _NoopIntel(),
        shadow_factory=lambda cfg, ws: None))

    assert len(calls) == 2 and len(agents) == 2
    # "/new /tmp" switches active -> slot 1, so "hello" ran there (the settle
    # line that gives poller-1's task a tick to start -- see the comment on
    # the read_line sequence above); slot 0's agent is untouched so far.
    assert agents[0].tasks == []
    assert agents[1].tasks == ["hello"]

    # Each captured poller's own `submit` is bound to ITS slot -- driving one
    # never touches the other's agent (was: one process-wide poller resolved
    # `_steer_target(slots)` fresh every tick, so which slot a remote
    # instruction landed in depended on whatever was active/first-session AT
    # THAT MOMENT; every slot now has its own fixed target, by construction).
    asyncio.run(calls[0]("into slot zero", "telegram", "iid-0"))
    assert agents[0].tasks == ["into slot zero"]
    assert agents[1].tasks == ["hello"]                # untouched by poller-0's submit

    asyncio.run(calls[1]("into slot one", "telegram", "iid-1"))
    assert agents[1].tasks == ["hello", "into slot one"]
    assert agents[0].tasks == ["into slot zero"]      # unaffected by the second submit


def test_two_session_slots_each_poller_is_tracked_in_its_own_bg_tasks_and_cancelled_on_exit(monkeypatch):
    import webbee.steer as SP
    fates = []

    async def hanging_poller(cfg, token_provider, **kw):
        fate = {"cancelled": False}
        fates.append(fate)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            fate["cancelled"] = True
            raise

    monkeypatch.setattr(SP, "poll_idle_steer", hanging_poller)
    # "hello" settles the just-scheduled second poller in (same reasoning as
    # the fan-out test above) before "/exit" tears it down.
    sink, agent = _run(read_line=_lines("/new /tmp", "hello", "/exit"), agent=SurfaceAgent())

    # No leaked tasks -- the exit-time teardown walks EVERY slot's own
    # bg_tasks (unchanged plumbing; W4b T5 just puts more into it).
    assert len(fates) == 2
    assert all(f["cancelled"] for f in fates)


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


def test_remote_downgrade_via_on_mode_persists_too(monkeypatch):
    # T6.1: the remote flip goes through set_slot_mode exactly like the
    # local /mode command -- the choice must survive to the next boot.
    import webbee.mode_store as mode_store
    _spy_on_mode(monkeypatch, ("plan", "telegram"))
    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent())
    assert mode_store.load_mode(os.getcwd()) == "plan"


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


def test_remote_autopilot_approval_never_persists_as_autopilot(monkeypatch):
    # Security posture (T6.1): even a locally-APPROVED remote autopilot
    # upgrade is downgraded to 'default' on disk -- the next boot in this
    # repo must NOT silently resume autopilot.
    import webbee.mode_store as mode_store
    _spy_on_mode(monkeypatch, ("autopilot", "telegram"))

    class ConfirmSink(FakeSink):
        async def ask_yes_no(self, question, timeout=60.0):
            return True

    sink, agent = _run(read_line=_lines("hello", "/exit"), agent=SurfaceAgent(),
                       sink=ConfirmSink())
    assert agent.mode == "autopilot"                  # live process mode IS autopilot
    assert mode_store.load_mode(os.getcwd()) == "default"   # but never remembered as such


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
# triple (wiring map §6); `_slot_ctx` is the pure, module-level extraction
# the run_repl closures (_ctx) read from -- driven directly here without
# needing to run the whole REPL loop.

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


def test_make_session_slot_shares_resources_bundle_on_same_workspace(monkeypatch):
    # Two slots opened on the SAME repo root, where auto-worktree isolation
    # is UNAVAILABLE (W4b T5 item 4's degrade path -- stubbed here so this
    # unit test never shells out `git worktree add` against the actual repo
    # checkout pytest runs from) must share ONE intel instance (map §6: same
    # workspace -> same intel/shadow/git_branch bundle) -- the intel_factory
    # must fire exactly once, not once per slot. The isolated-into-its-own-
    # worktree case (creation SUCCEEDS -> its own fresh bundle) is covered
    # separately below.
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "create_worktree", lambda root, slot_id: None)

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


# ── W4b T5 item 1: slot_id minting -- tab-1 legacy id vs later short hex ────

def test_make_session_slot_first_true_keeps_slot_id_empty():
    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=True)

    slot = asyncio.run(_drive())
    assert slot.slot_id == ""
    assert slot.agent.slot_id == ""


# ── 0.3.25 Part C: per-repo instance lock wiring in _make_session_slot ─────

class _FakeLock:
    def __init__(self, held):
        self.held = held
        self.closed = False

    def close(self):
        self.closed = True


def test_first_true_primary_lock_keeps_legacy_slot_id_and_rides_on_the_slot(monkeypatch):
    import webbee.instance_lock as IL
    lock = _FakeLock(held=False)
    monkeypatch.setattr(IL, "acquire", lambda repo_key: lock)

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=True)

    slot = asyncio.run(_drive())
    assert slot.slot_id == ""                # primary keeps the legacy id
    assert slot.instance_lock is lock         # rides on the slot for later teardown
    assert "already running" not in slot.pane.dump()


def test_first_true_secondary_lock_mints_slot_id_and_notes():
    async def _drive(lock):
        import webbee.instance_lock as IL
        orig = IL.acquire
        IL.acquire = lambda repo_key: lock
        try:
            return await _make_session_slot(
                _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
                resources=WorkspaceResources(), shared_client=None,
                agent_factory=lambda c, tp, ws, m: FakeAgent(),
                intel_factory=lambda cfg, ws: _NoopIntel(),
                shadow_factory=lambda cfg, ws: None, first=True)
        finally:
            IL.acquire = orig

    slot = asyncio.run(_drive(_FakeLock(held=True)))
    assert slot.slot_id and len(slot.slot_id) == 6   # minted, same as a later tab
    assert all(c in "0123456789abcdef" for c in slot.slot_id)
    assert slot.agent.slot_id == slot.slot_id
    dump = slot.pane.dump()
    assert "another Webbee is already running" in dump and "parallel session" in dump


def test_first_false_never_touches_the_instance_lock_at_all(monkeypatch):
    import webbee.instance_lock as IL
    calls = []
    monkeypatch.setattr(IL, "acquire", lambda repo_key: calls.append(repo_key) or _FakeLock(False))

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, "/tmp", "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    slot = asyncio.run(_drive())
    assert calls == []                         # acquire() never called for a later tab
    assert getattr(slot, "instance_lock", None) is None


def test_explicit_slot_id_survives_a_secondary_lock_unchanged(monkeypatch):
    # A caller-supplied slot_id (the deterministic-test DI seam) must never
    # be overridden by the lock's own verdict -- only an AUTO-minted id
    # (the real production path) is subject to it.
    import webbee.instance_lock as IL
    monkeypatch.setattr(IL, "acquire", lambda repo_key: _FakeLock(held=True))

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=True, slot_id="pinned")

    slot = asyncio.run(_drive())
    assert slot.slot_id == "pinned"
    # the secondary note still lands -- only the id-minting is skipped
    assert "already running" in slot.pane.dump()


def test_make_session_slot_first_false_mints_a_short_hex_slot_id():
    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, "/tmp", "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    slot = asyncio.run(_drive())
    assert slot.slot_id and len(slot.slot_id) == 6
    assert all(c in "0123456789abcdef" for c in slot.slot_id)
    assert slot.agent.slot_id == slot.slot_id   # threaded onto the agent too


def test_make_session_slot_mints_a_different_id_each_time():
    async def _drive():
        s1 = await _make_session_slot(
            _mk_cfg(), _noop_token_provider, "/tmp", "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)
        s2 = await _make_session_slot(
            _mk_cfg(), _noop_token_provider, "/tmp", "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)
        return s1, s2

    s1, s2 = asyncio.run(_drive())
    assert s1.slot_id != s2.slot_id


def test_make_session_slot_explicit_slot_id_overrides_auto_mint():
    # DI seam for deterministic tests (worktree naming, poller derivation).
    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, "/tmp", "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False, slot_id="fixed1")

    slot = asyncio.run(_drive())
    assert slot.slot_id == "fixed1"


# ── W4b T5 item 4: auto-worktree for a same-repo second tab ─────────────────

def test_isolate_workspace_skips_when_first():
    resources = WorkspaceResources()
    resources.put("/repo", {"intel": None})   # pretend a bundle already exists
    ws, note = asyncio.run(_isolate_workspace("/repo", resources, first=True, slot_id="abc123"))
    assert (ws, note) == ("/repo", "")


def test_isolate_workspace_skips_when_no_existing_bundle_for_this_root():
    # A DIFFERENT repo -- resources has nothing cached for it yet, so this
    # is treated as a genuinely first-of-its-kind workspace, not isolated.
    resources = WorkspaceResources()
    ws, note = asyncio.run(_isolate_workspace("/repo", resources, first=False, slot_id="abc123"))
    assert (ws, note) == ("/repo", "")


def test_isolate_workspace_creates_a_worktree_when_a_bundle_already_exists(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    resources = WorkspaceResources()
    resources.put(str(tmp_path), {"intel": None})   # simulates an existing session slot's bundle
    monkeypatch.setattr(WT, "create_worktree", lambda root, slot_id: f"/wt/{slot_id}")

    ws, note = asyncio.run(_isolate_workspace(str(tmp_path), resources, first=False, slot_id="abc123"))
    assert ws == "/wt/abc123"
    assert "isolated worktree" in note


def test_isolate_workspace_degrades_to_shared_checkout_on_worktree_failure(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    resources = WorkspaceResources()
    resources.put(str(tmp_path), {"intel": None})
    monkeypatch.setattr(WT, "create_worktree", lambda root, slot_id: None)

    ws, note = asyncio.run(_isolate_workspace(str(tmp_path), resources, first=False, slot_id="abc123"))
    assert ws == str(tmp_path)
    assert "shared checkout" in note


def _init_real_git_repo(tmp_path, name="proj"):
    import subprocess
    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return root


def test_second_session_slot_on_same_repo_gets_isolated_into_a_real_worktree(tmp_path):
    # End-to-end (real git subprocess) through _make_session_slot: a genuine
    # SECOND tab on a repo tab-1 already opened gets its own worktree, with
    # an honest note in its own pane, and its tab title stays the ORIGINAL
    # repo's name (never the worktree cache path's basename).
    root = _init_real_git_repo(tmp_path)
    import webbee.worktrees as WT
    monkeypatch_root = str(tmp_path / "cache" / "worktrees")
    orig_root = WT.WORKTREE_ROOT
    WT.WORKTREE_ROOT = monkeypatch_root
    try:
        resources = WorkspaceResources()

        async def _drive():
            s1 = await _make_session_slot(
                _mk_cfg(), _noop_token_provider, str(root), "default",
                resources=resources, shared_client=None,
                agent_factory=lambda c, tp, ws, m: FakeAgent(),
                intel_factory=lambda cfg, ws: _NoopIntel(),
                shadow_factory=lambda cfg, ws: None, first=True)
            s2 = await _make_session_slot(
                _mk_cfg(), _noop_token_provider, str(root), "default",
                resources=resources, shared_client=None,
                agent_factory=lambda c, tp, ws, m: FakeAgent(),
                intel_factory=lambda cfg, ws: _NoopIntel(),
                shadow_factory=lambda cfg, ws: None, first=False)
            return s1, s2

        s1, s2 = asyncio.run(_drive())
    finally:
        WT.WORKTREE_ROOT = orig_root

    assert s2.workspace != s1.workspace
    assert s2.workspace != str(root)
    assert os.path.isdir(s2.workspace)
    assert s2.label == s1.label == "proj"          # tab title unaffected by the swap
    assert "isolated worktree" in s2.pane.dump()


def test_second_session_slot_on_a_different_repo_is_not_isolated(tmp_path):
    # tab-1 opens repo A; a SECOND tab on a totally different repo B must
    # never be routed through worktree isolation at all -- different root,
    # nothing "already open" there.
    root_a = _init_real_git_repo(tmp_path, name="a")
    root_b = _init_real_git_repo(tmp_path, name="b")
    import webbee.worktrees as WT
    orig_root = WT.WORKTREE_ROOT
    WT.WORKTREE_ROOT = str(tmp_path / "cache" / "worktrees")
    try:
        resources = WorkspaceResources()

        async def _drive():
            s1 = await _make_session_slot(
                _mk_cfg(), _noop_token_provider, str(root_a), "default",
                resources=resources, shared_client=None,
                agent_factory=lambda c, tp, ws, m: FakeAgent(),
                intel_factory=lambda cfg, ws: _NoopIntel(),
                shadow_factory=lambda cfg, ws: None, first=True)
            s2 = await _make_session_slot(
                _mk_cfg(), _noop_token_provider, str(root_b), "default",
                resources=resources, shared_client=None,
                agent_factory=lambda c, tp, ws, m: FakeAgent(),
                intel_factory=lambda cfg, ws: _NoopIntel(),
                shadow_factory=lambda cfg, ws: None, first=False)
            return s1, s2

        s1, s2 = asyncio.run(_drive())
    finally:
        WT.WORKTREE_ROOT = orig_root

    assert s2.workspace == str(root_b)             # untouched -- own, never-before-seen root
    assert "isolated worktree" not in s2.pane.dump()
    assert "shared checkout" not in s2.pane.dump()


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


# ── T6.1: _make_session_slot loads a repo's remembered mode ──────────────────

def test_make_session_slot_uses_remembered_mode_over_process_baseline(monkeypatch):
    import webbee.mode_store as MS
    monkeypatch.setattr(MS, "load_mode", lambda ws: "plan")
    captured = {}

    def agent_factory(c, tp, ws, m):
        captured["mode"] = m
        return FakeAgent()

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=agent_factory, intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    slot = asyncio.run(_drive())
    assert slot.mode == "plan"          # remembered mode wins over the "default" baseline
    assert captured["mode"] == "plan"   # the agent itself is built with it too


def test_make_session_slot_mode_pinned_beats_remembered_mode(monkeypatch):
    # W5 fix: a new tab opened from Home carries the user's EXPLICIT "new-tab
    # mode" (mode_pinned=True) which must win over this repo's remembered
    # mode. Before this, load_mode silently overrode the picked mode —
    # inconsistently by target repo — so Ctrl+T opened with the wrong mode
    # while typing on Home happened to keep it. This is the shared chokepoint
    # both _open_new_tab (Ctrl+T / + / /new) and _home_input (typing) use.
    import webbee.mode_store as MS
    monkeypatch.setattr(MS, "load_mode", lambda ws: "plan")
    captured = {}

    def agent_factory(c, tp, ws, m):
        captured["mode"] = m
        return FakeAgent()

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "autopilot",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=agent_factory, intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False, mode_pinned=True)

    slot = asyncio.run(_drive())
    assert slot.mode == "autopilot"        # pinned explicit choice beats remembered "plan"
    assert captured["mode"] == "autopilot"


def test_make_session_slot_falls_back_to_baseline_when_nothing_remembered(monkeypatch):
    import webbee.mode_store as MS
    monkeypatch.setattr(MS, "load_mode", lambda ws: None)

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "default",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    slot = asyncio.run(_drive())
    assert slot.mode == "default"


def test_make_session_slot_never_loads_autopilot_from_cache(monkeypatch):
    # save_mode already refuses to write 'autopilot' to disk, so load_mode
    # can never legitimately return it -- but the wiring must not special-
    # case it either way: whatever load_mode returns is used verbatim.
    import webbee.mode_store as MS
    monkeypatch.setattr(MS, "load_mode", lambda ws: None)   # the realistic case

    async def _drive():
        return await _make_session_slot(
            _mk_cfg(), _noop_token_provider, os.getcwd(), "autopilot",
            resources=WorkspaceResources(), shared_client=None,
            agent_factory=lambda c, tp, ws, m: FakeAgent(),
            intel_factory=lambda cfg, ws: _NoopIntel(),
            shadow_factory=lambda cfg, ws: None, first=False)

    slot = asyncio.run(_drive())
    assert slot.mode == "autopilot"   # an EXPLICIT process baseline is still honored


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


def test_cancel_slot_flags_turn_stopped_before_cancelling():
    # FIX2 (ghost drain on close): closing a busy tab must flag
    # turn["stopped"] = True -- the SAME "user is taking control" marker
    # Esc/Ctrl-C set -- so tui's _run_turn finally block holds the queue
    # instead of draining it into a brand-new turn on a slot that no longer
    # exists in the SlotManager.
    slot = SessionSlot(kind="session", workspace=".", label="t",
                       pane=object(), sink=None, agent=None)
    slot.turn["task"] = _FakeTask()
    slot.pending.extend(["queued 1", "queued 2"])

    _cancel_slot(slot)

    assert slot.turn.get("stopped") is True
    assert slot.turn["task"].cancelled is True
    assert list(slot.pending) == ["queued 1", "queued 2"]   # untouched -- dies with the slot


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


# ── W4c T3: self-naming tabs -- auto-label from the first task ─────────────

def test_first_task_auto_labels_the_tab():
    from webbee.slots import auto_label
    text = "please help me fix the failing auth test suite"
    sink, agent = _run(read_line=_lines(text, "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert auto_label(text) in listing
    assert text not in listing              # genuinely shortened, never verbatim


def test_short_first_task_becomes_the_label_verbatim():
    text = "fix the bug"
    sink, agent = _run(read_line=_lines(text, "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert text in listing


def test_second_task_never_relabels_an_already_auto_labeled_tab():
    from webbee.slots import auto_label
    sink, agent = _run(read_line=_lines("first task about the auth flow",
                                        "second completely unrelated topic",
                                        "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert auto_label("first task about the auth flow") in listing
    assert "unrelated" not in listing


def test_slash_command_first_line_never_counts_as_the_first_task():
    # /status is fully handled by dispatch and never reaches the agent turn
    # path at all -- the FIRST genuine task line is the one that labels.
    text = "fix the thing"
    sink, agent = _run(read_line=_lines("/status", text, "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert text in listing


# ── /rename — a manual rename always wins over auto-label ──────────────────

def test_rename_command_sets_the_slots_label():
    sink, agent = _run(read_line=_lines("/rename billing fix", "/tabs", "/exit"))
    assert any("tab renamed" in n and "billing fix" in n for n in sink.notes)
    listing = sink.notes[-1]
    assert "billing fix" in listing


def test_rename_with_no_arg_shows_usage():
    sink, agent = _run(read_line=_lines("/rename", "/exit"))
    assert any("Usage: /rename" in n for n in sink.notes)


def test_rename_before_first_task_blocks_auto_label():
    sink, agent = _run(read_line=_lines("/rename billing", "unrelated task text here",
                                        "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert "billing" in listing
    assert "unrelated" not in listing


def test_rename_after_first_task_overrides_the_auto_label():
    from webbee.slots import auto_label
    first = "please help me fix the failing auth suite"
    sink, agent = _run(read_line=_lines(first, "/rename billing", "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert "billing" in listing
    assert auto_label(first) not in listing


def test_rename_sanitizes_and_caps_the_name():
    sink, agent = _run(read_line=_lines("/rename " + "x" * 40, "/tabs", "/exit"))
    listing = sink.notes[-1]
    assert ("x" * 32) in listing
    assert ("x" * 33) not in listing


# ── W4a Task 7: multi-tab edges -- exit dump, cancellation ───────────────────
# _exit_dump/_cancel_all_background are module-level and pure (same
# DI-testing philosophy as _gate_busy/_cancel_slot) so each is driven
# directly here without needing a live dock or the fallback loop's
# single-slot world. (Steer-poller TARGETING no longer needs a dedicated
# routing helper at all -- W4b T5 gives every session slot its OWN poller,
# bound to that slot directly; see test_steer_pickup_* below.)

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


# ── W4a final-review FIX1: cross-tab execution -- the slot is threaded
# through the on_line boundary end to end. `_handle`/`_run_turn` used to
# resolve `slots.active()` internally, so a drain (or the turn itself) ran
# in whatever tab happened to be VISIBLE by the time its background task's
# body actually executed, not the tab it was typed into. These two tests
# drive the REAL dock through `run_repl` (sys.stdin.isatty forced True,
# wrapped in a genuine prompt_toolkit pipe-input session) so the actual repl
# closures are what's under test -- not a hand-written double standing in
# for them (unlike the tui-level `test_on_line_receives_the_pinned_slot_
# never_whatever_becomes_active_later` above, which only proves tui's OWN
# half of the contract).

def _spy_output_panes(monkeypatch):
    """Records every OutputPane the dock creates, in creation order (Home
    first, then each session slot as it's made) -- gives a test a handle on
    a SPECIFIC slot's own scrollback (`pane.dump()`) without needing to reach
    into the closure-private SlotManager `run_repl` builds."""
    from webbee import tui
    created = []

    class _SpyPane(tui.OutputPane):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    monkeypatch.setattr(tui, "OutputPane", _SpyPane)
    return created


def _mute_dock_background_io(monkeypatch):
    """Test hygiene for a real-dock run: no steer polling, no PyPI update
    check, no real filesystem watcher against `_NoopIntel`'s fake root --
    all three are best-effort background tasks a dock boot always starts,
    and none of them should touch the network (or race a real dock's
    multi-second lifetime against `watchfiles` raising on a nonexistent
    path, `_NoopIntel.root`) just because this test drives the real dock
    instead of the fallback loop."""
    import webbee.steer as SP
    import webbee.update as UP
    from webbee.intel import watch as WATCH

    async def noop_poller(cfg, token_provider, **kw):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    async def hanging_watch(root, on_change):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(SP, "poll_idle_steer", noop_poller)
    monkeypatch.setattr(UP, "default_fetch", lambda: None)
    monkeypatch.setattr(WATCH, "watch_workspace", hanging_watch)


async def _until(pred, timeout=5.0):
    import time
    t0 = time.time()
    while not pred():
        assert time.time() - t0 < timeout, "timed out"
        await asyncio.sleep(0.01)


def test_real_dock_turn_and_drain_stay_pinned_to_the_originating_slot(monkeypatch):
    # Turn runs in slot A; the user switches active to slot B mid-turn;
    # natural completion drains A's OWN queued line -- it must echo and run
    # IN A (A's agent, A's pane), never in B, even though B is what's on
    # screen the instant the drain's background task body executes.
    import sys

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    _mute_dock_background_io(monkeypatch)
    created_panes = _spy_output_panes(monkeypatch)

    gate = asyncio.Event()
    agents = []

    class GatedAgent(FakeAgent):
        async def run(self, task, sink, *, marathon=False, goal="", surface="", steer_iid=""):
            self.tasks.append(task)
            self.runs.append({"task": task})
            await gate.wait()
            return f"answer:{task}"

    def agent_factory(cfg, tp, ws, mode):
        a = GatedAgent() if not agents else FakeAgent()
        agents.append(a)
        return a

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def scenario():
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(run_repl(
                    cfg, "default", agent_factory=agent_factory,
                    auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
                    sessions_client=FakeSessions(), intel_factory=lambda c, ws: _NoopIntel(),
                    shadow_factory=lambda c, ws: None))
                await asyncio.sleep(0.1)   # boot: Home(0) + slot A(1), active=1

                pipe.send_text("/new /tmp\r")          # opens slot B(2), auto-switches active->2
                await _until(lambda: len(agents) == 2)
                await asyncio.sleep(0.05)

                pipe.send_text("\x1b1")                 # Alt+1 -- back to A
                await _until(lambda: created_panes and True)
                await asyncio.sleep(0.05)

                pipe.send_text("first\r")               # starts a (gated) turn IN A
                await _until(lambda: agents[0].tasks == ["first"])

                pipe.send_text("queued-in-a\r")         # busy(A) -> local queue (no live session_id)
                await asyncio.sleep(0.1)

                pipe.send_text("\x1b2")                 # Alt+2 -- switch to B mid-turn
                await asyncio.sleep(0.1)

                gate.set()                              # A's turn completes naturally
                await _until(lambda: agents[0].tasks == ["first", "queued-in-a"])
                await asyncio.sleep(0.05)

                pane_a, pane_b = created_panes[1], created_panes[2]
                assert "queued-in-a" in pane_a.dump()          # the drained echo landed in A
                assert "queued-in-a" not in pane_b.dump()      # B's own pane never touched
                assert agents[1].tasks == []                   # B's own agent never ran anything

                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_real_dock_enter_started_turn_lands_in_the_slot_captured_at_keypress(monkeypatch):
    # A FRESH (non-drain) turn: pressing Enter on slot A must land the echo
    # and the agent.run call in A, even when the switch-to-B keystroke is
    # sent immediately after (back to back, no await in between) -- i.e.
    # BEFORE the scheduled background task's body has had any chance to run
    # its own on_line/_handle call and re-derive "whatever is active now".
    import sys

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    _mute_dock_background_io(monkeypatch)
    created_panes = _spy_output_panes(monkeypatch)

    gate = asyncio.Event()
    agents = []

    class GatedAgent(FakeAgent):
        async def run(self, task, sink, *, marathon=False, goal="", surface="", steer_iid=""):
            self.tasks.append(task)
            await gate.wait()
            return f"answer:{task}"

    def agent_factory(cfg, tp, ws, mode):
        a = GatedAgent() if not agents else FakeAgent()
        agents.append(a)
        return a

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def scenario():
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(run_repl(
                    cfg, "default", agent_factory=agent_factory,
                    auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
                    sessions_client=FakeSessions(), intel_factory=lambda c, ws: _NoopIntel(),
                    shadow_factory=lambda c, ws: None))
                await asyncio.sleep(0.1)   # boot: Home(0) + slot A(1), active=1

                pipe.send_text("/new /tmp\r")          # opens slot B(2), auto-switches active->2
                await _until(lambda: len(agents) == 2)
                await asyncio.sleep(0.05)

                pipe.send_text("\x1b1")                 # back to A
                await asyncio.sleep(0.05)

                # No await between these two sends: both land in the pipe's
                # buffer before the event loop gets a chance to run anything,
                # simulating the switch happening strictly between the Enter
                # key handler (which only SCHEDULES the turn's background
                # task) and that task's body actually starting.
                pipe.send_text("first\r")
                pipe.send_text("\x1b2")

                await _until(lambda: agents[0].tasks == ["first"])
                await asyncio.sleep(0.05)

                pane_a, pane_b = created_panes[1], created_panes[2]
                assert "first" in pane_a.dump()             # the echo landed in A, not B
                assert "first" not in pane_b.dump()
                assert agents[1].tasks == []                 # B's own agent never touched

                gate.set()
                await asyncio.sleep(0.05)
                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


# ── W4a final-review FIX4: Home None-sink command crashes ───────────────────
# Home's sink is None -- dispatching a command while Home is active used to
# crash on an unguarded `_sink.note`/`_sink.clear()`, or (for the few call
# sites already `if _sink is not None:`-guarded) silently swallow the reply
# instead of showing one. `_say(slot, msg)` fixes both: a real session's
# `sink.note` unchanged, Home's own pane console otherwise. These drive the
# REAL dock (same harness as FIX1/FIX3 above) so the actual `_handle` action
# ladder is what's under test.

def test_home_active_help_renders_into_the_home_pane(monkeypatch):
    import sys

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    _mute_dock_background_io(monkeypatch)
    created_panes = _spy_output_panes(monkeypatch)

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def scenario():
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(run_repl(
                    cfg, "default", agent_factory=lambda c, tp, ws, m: FakeAgent(),
                    auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
                    sessions_client=FakeSessions(), intel_factory=lambda c, ws: _NoopIntel(),
                    shadow_factory=lambda c, ws: None))
                await asyncio.sleep(0.1)          # boot: Home(0) + slot A(1), active=1

                pipe.send_text("\x1b0")            # Alt+0 -- switch to Home (slot 0)
                await asyncio.sleep(0.05)

                pipe.send_text("/help\r")
                pane_home = created_panes[0]
                # poll (not a fixed sleep): help text landed in Home's OWN pane
                await _until(lambda: "show this help" in pane_home.dump())

                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_home_active_steps_yields_open_a_tab_note(monkeypatch):
    import sys

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    _mute_dock_background_io(monkeypatch)
    created_panes = _spy_output_panes(monkeypatch)

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def scenario():
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(run_repl(
                    cfg, "default", agent_factory=lambda c, tp, ws, m: FakeAgent(),
                    auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
                    sessions_client=FakeSessions(), intel_factory=lambda c, ws: _NoopIntel(),
                    shadow_factory=lambda c, ws: None))
                await asyncio.sleep(0.1)

                pipe.send_text("\x1b0")            # Alt+0 -- switch to Home (slot 0)
                await asyncio.sleep(0.05)

                pipe.send_text("/steps\r")          # session-specific -- must not crash
                pane_home = created_panes[0]
                await _until(lambda: "open a session tab first" in pane_home.dump())

                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_home_active_tabs_lists_tabs(monkeypatch):
    import sys

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    _mute_dock_background_io(monkeypatch)
    created_panes = _spy_output_panes(monkeypatch)

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def scenario():
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(run_repl(
                    cfg, "default", agent_factory=lambda c, tp, ws, m: FakeAgent(),
                    auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
                    sessions_client=FakeSessions(), intel_factory=lambda c, ws: _NoopIntel(),
                    shadow_factory=lambda c, ws: None))
                await asyncio.sleep(0.1)

                pipe.send_text("\x1b0")            # Alt+0 -- switch to Home (slot 0)
                await asyncio.sleep(0.05)

                pipe.send_text("/tabs\r")
                pane_home = created_panes[0]
                # Poll rather than a fixed sleep (W4b T5 added heavier
                # subprocess-backed tests to this same file -- occasional
                # thread-pool contention could otherwise starve a fixed
                # budget); _until is the SAME robustness pattern every other
                # real-dock scenario in this file already uses.
                await _until(lambda: "Open tabs:" in pane_home.dump())
                assert "●0" in pane_home.dump()     # Home itself listed as active

                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


# ── W4a final-review FIX7e: land-on-Home ─────────────────────────────────────
# boot.replay_thread now returns the count of replayed display messages
# (0 on skip/error, keeping the never-raise contract) -- the dock boot uses
# it to land on the session tab only when the replay actually showed
# something; a fresh/empty thread lands on Home instead (Alt+1 away).

def _spy_slot_manager(monkeypatch):
    """Records every SlotManager `run_repl` constructs, so a test can
    inspect its `active_idx` after boot without needing a reference the
    closure never hands out."""
    import webbee.repl as repl_mod
    from webbee.slots import SlotManager as _RealSlotManager
    created = []

    class _SpySlotManager(_RealSlotManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    monkeypatch.setattr(repl_mod, "SlotManager", _SpySlotManager)
    return created


def test_land_on_home_when_boot_replay_is_fresh_and_empty(monkeypatch):
    import sys

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    import imperal_mcp.client as ic
    import webbee.thread as TH

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    async def fake_fetch(cfg, token_provider, session_id):
        return []                                   # fresh/empty thread

    monkeypatch.setattr(TH, "fetch_recent_thread", fake_fetch)

    _mute_dock_background_io(monkeypatch)
    created_slots = _spy_slot_manager(monkeypatch)

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def scenario():
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(run_repl(
                    cfg, "default", agent_factory=lambda c, tp, ws, m: FakeAgent(),
                    auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
                    sessions_client=FakeSessions(), intel_factory=lambda c, ws: _NoopIntel(),
                    shadow_factory=lambda c, ws: None))
                await asyncio.sleep(0.15)
                assert created_slots and created_slots[0].active_idx == 0   # landed on Home

                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_land_on_session_when_boot_replay_shows_something(monkeypatch):
    import sys

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    import imperal_mcp.client as ic
    import webbee.thread as TH

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    async def fake_fetch(cfg, token_provider, session_id):
        return [{"role": "assistant", "content": "done", "surface": "terminal"}]

    monkeypatch.setattr(TH, "fetch_recent_thread", fake_fetch)

    _mute_dock_background_io(monkeypatch)
    created_slots = _spy_slot_manager(monkeypatch)

    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def scenario():
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(run_repl(
                    cfg, "default", agent_factory=lambda c, tp, ws, m: FakeAgent(),
                    auth=FakeAuth(), account_fetcher=_fake_account_fetcher,
                    sessions_client=FakeSessions(), intel_factory=lambda c, ws: _NoopIntel(),
                    shadow_factory=lambda c, ws: None))
                await asyncio.sleep(0.15)
                assert created_slots and created_slots[0].active_idx == 1   # landed on the session

                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_replay_thread_returns_shown_count_and_zero_on_failure(monkeypatch):
    # Unit-level companion: boot.replay_thread's own new return contract,
    # driven directly (no dock needed) -- 0 on a fresh/empty thread AND on
    # any failure, matching the never-raise contract; the actual count on
    # a real replay.
    import imperal_mcp.client as ic
    import webbee.thread as TH
    from webbee import boot as BOOT
    from webbee.config import Config

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)
    cfg = Config(api_url="http://x", panel_url="http://p")

    async def _tp():
        return "tok"

    async def fake_fetch_two(cfg, tp, session_id):
        return [{"role": "user", "content": "hi", "surface": "terminal"},
               {"role": "assistant", "content": "done", "surface": "terminal"}]

    monkeypatch.setattr(TH, "fetch_recent_thread", fake_fetch_two)
    n = asyncio.run(BOOT.replay_thread(cfg, _tp, FakeSink()))
    assert n == 2

    async def fake_fetch_empty(cfg, tp, session_id):
        return []

    monkeypatch.setattr(TH, "fetch_recent_thread", fake_fetch_empty)
    assert asyncio.run(BOOT.replay_thread(cfg, _tp, FakeSink())) == 0

    async def boom(cfg, tp, session_id):
        raise RuntimeError("offline")

    monkeypatch.setattr(TH, "fetch_recent_thread", boom)
    assert asyncio.run(BOOT.replay_thread(cfg, _tp, FakeSink())) == 0




def test_remote_mode_flip_targets_polled_slot_not_active(monkeypatch):
    """v0.3.21 regression (Valentin live 2026-07-20): the remote req_mode flip
    lands on the POLLED session slot even when the sink-less Home tab is
    active — the old slots.active() targeting crashed on Home (None sink) and
    the one-shot GETDEL request was lost; the panel kept showing default.

    W4b T5: _on_mode/_confirm_autopilot moved to module level and now take
    the polled slot as an EXPLICIT parameter (one poller per session slot,
    each bound to its OWN slot directly) -- there's no more slots.active()/
    shared first_session_slot resolution to get wrong in the first place."""
    import inspect
    import webbee.repl as repl_mod

    on_mode_src = inspect.getsource(repl_mod._on_mode)
    assert "slots.active()" not in on_mode_src, "flip must not target the active tab"
    assert "def _on_mode(slot" in on_mode_src, "the polled slot is an explicit param"
    assert "_say(slot" in on_mode_src, "notes must be Home/None-sink safe"

    confirm_src = inspect.getsource(repl_mod._confirm_autopilot)
    assert "slots.active()" not in confirm_src
    assert "def _confirm_autopilot(slot" in confirm_src
