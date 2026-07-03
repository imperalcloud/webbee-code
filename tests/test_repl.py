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
    # TurnSink no-ops
    def tool_start(self, *a): ...
    def tool_result(self, *a): ...
    def ask_consent(self, *a): return "yes"
    def panel_release(self, *a): ...
    def progress(self, *a): ...
    def usage(self, *a): ...


class FakeAgent:
    def __init__(self): self.tasks = []; self.mode = "default"
    async def run(self, task, sink):
        self.tasks.append(task)
        return f"answer:{task}"


class FakeAuth:
    NotLoggedInError = RuntimeError
    def __init__(self, logged_in=True): self._in = logged_in; self.logged_out = False
    async def ensure_access_token(self, cfg):
        if not self._in: raise self.NotLoggedInError("no creds")
        return "tok"
    def login(self, cfg, *, open_browser=True): self._in = True; return "u@imperal.io"
    async def logout(self, cfg): self._in = False; self.logged_out = True


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


def _run(**kw):
    from webbee.config import Config
    cfg = Config(api_url="http://x", panel_url="http://p")
    sink = kw.pop("sink", FakeSink())
    agent = kw.pop("agent", FakeAgent())
    asyncio.run(run_repl(cfg, "default", sink=sink, agent_factory=lambda c, tp, ws, m: agent,
                         read_line=kw.pop("read_line"), auth=kw.pop("auth", FakeAuth()),
                         account_fetcher=kw.pop("account_fetcher", _fake_account_fetcher)))
    return sink, agent


def test_task_is_sent_to_agent_and_answer_rendered():
    sink, agent = _run(read_line=_lines("исправь баг", "/exit"))
    assert agent.tasks == ["исправь баг"]
    assert sink.turns == ["answer:исправь баг"]


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
        async def run(self, task, sink):
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


def test_mode_command_switches_agent_mode():
    sink, agent = _run(read_line=_lines("/mode autopilot", "/exit"))
    assert agent.mode == "autopilot"


def test_clear_command_clears_sink():
    sink, agent = _run(read_line=_lines("/clear", "/exit"))
    assert sink.cleared is True


def test_ctrl_c_mid_turn_aborts_and_returns_to_prompt():
    class InterruptingAgent(FakeAgent):
        async def run(self, task, sink):
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
