"""The terminal says it TOOK a request before it runs it (the kernel's rule 5, 2026-11-26).

Live 2026-11-26: a bash request was published and never answered; its session stood 18 minutes -- between "the
kernel sent it" and "the client began" there was no fact. A request frame that asks for the word (``ack``) is
answered at once through the door a result takes; a frame that does not ask is run as before; a request of
another turn is neither run nor acknowledged -- so the kernel hears in seconds that nobody took it."""
import asyncio

from tests.test_session import _FakeCfg


def _run(monkeypatch, frames, post_ack_raises=False):
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST
    import webbee.tools as T

    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": root, "git": "", "tree": "", "repo_key": "x", "repo_root": root})

    class _Imperal:
        def __init__(self, cfg, token_provider): pass
        async def whoami(self): return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _Imperal)
    events = []

    class _Exec:
        def __init__(self, root, indexer=None, shadow=None, client_factory=None): pass
        def run(self, tool, args):
            events.append(("run", tool))
            return {"ok": True, "content": "ran"}

    monkeypatch.setattr(T, "LocalToolExecutor", _Exec)

    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        for f in frames:
            yield f

    monkeypatch.setattr(ST, "stream_frames", _stream)

    class _Resp:
        def __init__(self, body): self.status_code, self._b = 200, body
        def raise_for_status(self): pass
        def json(self): return self._b

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, path, headers=None, json=None, **kw):
            if path == "/v1/agent/sessions":
                return _Resp({"session_id": "sid1", "last_id": "0-0", "task_id": "OURS"})
            res = (json or {}).get("result") or {}
            if res.get("state") == "ack":
                events.append(("ack", json["req_id"]))
                if post_ack_raises:
                    raise RuntimeError("gateway down")
            else:
                events.append(("result", json["req_id"], res.get("ok")))
            return _Resp({})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    class _Sink:
        def tool_start(self, *a): ...
        def tool_result(self, *a, **k): ...
        def progress(self, *a): ...
        def usage(self, *a): ...
        def foreign_turn(self, *a): ...

    async def token():
        return "tok"

    sess = S.AgentSession(cfg=_FakeCfg(), token_provider=token, workspace_root=".")
    out = asyncio.run(sess.run("do it", _Sink()))
    return out, events


def test_a_request_that_asks_is_acknowledged_before_it_runs(monkeypatch):
    out, ev = _run(monkeypatch, [
        {"type": "tool_request", "task_id": "OURS", "req_id": "r1", "tool": "read_file", "args": {}, "ack": 1},
        {"type": "final", "task_id": "OURS", "text": "done"}])
    assert out == "done"
    assert ev == [("ack", "r1"), ("run", "read_file"), ("result", "r1", True)]


def test_a_request_that_does_not_ask_runs_as_before(monkeypatch):
    _, ev = _run(monkeypatch, [
        {"type": "tool_request", "task_id": "OURS", "req_id": "r1", "tool": "read_file", "args": {}},
        {"type": "final", "task_id": "OURS", "text": "done"}])
    assert ev == [("run", "read_file"), ("result", "r1", True)]


def test_a_request_sent_again_is_answered_with_its_result_not_run_or_acknowledged_twice(monkeypatch):
    req = {"type": "tool_request", "task_id": "OURS", "req_id": "r1", "tool": "read_file", "args": {}, "ack": 1}
    _, ev = _run(monkeypatch, [req, dict(req), {"type": "final", "task_id": "OURS", "text": "done"}])
    assert ev == [("ack", "r1"), ("run", "read_file"), ("result", "r1", True), ("result", "r1", True)]


def test_a_request_of_another_turn_is_neither_run_nor_acknowledged(monkeypatch):
    _, ev = _run(monkeypatch, [
        {"type": "tool_request", "task_id": "OTHER", "req_id": "x1", "tool": "bash", "args": {}, "ack": 1},
        {"type": "final", "task_id": "OURS", "text": "done"}])
    assert ev == []                       # the kernel hears no word for x1: it says so in seconds, not in an hour


def test_a_word_that_could_not_be_sent_never_holds_the_tool_back(monkeypatch):
    _, ev = _run(monkeypatch, [
        {"type": "tool_request", "task_id": "OURS", "req_id": "r1", "tool": "read_file", "args": {}, "ack": 1},
        {"type": "final", "task_id": "OURS", "text": "done"}], post_ack_raises=True)
    assert ev == [("ack", "r1"), ("run", "read_file"), ("result", "r1", True)]
