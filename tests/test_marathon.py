"""U4 marathon launch (client side).

Covers the two SECURITY/behaviour-load-bearing pieces:
  * detect_verify_cmd — the CLIENT-detected proof-of-done command per ecosystem
    (the kernel runs ONLY this; the brain never authors a shell command).
  * --marathon <goal> — flags the outgoing request marathon=True + goal=<goal>
    and attaches coding_context["verify_cmd"]; the coding path is unchanged.
  * marathon FACT frames render one-liners and never crash an unknown-frame /
    note-less sink.
"""
import asyncio

from webbee.session import detect_verify_cmd


# --- detect_verify_cmd -------------------------------------------------------

def test_detect_verify_cmd_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'\n")
    assert detect_verify_cmd(str(tmp_path)) == "pytest -q"


def test_detect_verify_cmd_setup_cfg(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n")
    assert detect_verify_cmd(str(tmp_path)) == "pytest -q"


def test_detect_verify_cmd_tox(tmp_path):
    (tmp_path / "tox.ini").write_text("[tox]\n")
    assert detect_verify_cmd(str(tmp_path)) == "pytest -q"


def test_detect_verify_cmd_npm_with_test_script(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert detect_verify_cmd(str(tmp_path)) == "npm test"


def test_detect_verify_cmd_npm_without_test_script_is_empty(tmp_path):
    # package.json present but no "test" script — must NOT claim npm test.
    (tmp_path / "package.json").write_text('{"scripts": {"build": "tsc"}}')
    assert detect_verify_cmd(str(tmp_path)) == ""


def test_detect_verify_cmd_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert detect_verify_cmd(str(tmp_path)) == "cargo test"


def test_detect_verify_cmd_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_verify_cmd(str(tmp_path)) == "go test ./..."


def test_detect_verify_cmd_makefile_with_test_target(tmp_path):
    (tmp_path / "Makefile").write_text(".PHONY: test\ntest:\n\tpytest\n")
    assert detect_verify_cmd(str(tmp_path)) == "make test"


def test_detect_verify_cmd_makefile_without_test_target_is_empty(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n")
    assert detect_verify_cmd(str(tmp_path)) == ""


def test_detect_verify_cmd_empty_dir(tmp_path):
    assert detect_verify_cmd(str(tmp_path)) == ""


def test_detect_verify_cmd_priority_pyproject_over_npm(tmp_path):
    # Polyglot repo: python runner wins per the fixed priority order.
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert detect_verify_cmd(str(tmp_path)) == "pytest -q"


# --- --marathon parsing + request payload ------------------------------------

def test_parser_marathon_captures_goal():
    from webbee.cli import build_parser
    args = build_parser().parse_args(["--marathon", "build X"])
    assert args.marathon == "build X"


def test_parser_marathon_defaults_none():
    from webbee.cli import build_parser
    assert build_parser().parse_args([]).marathon is None


def _run_marathon_capture_post(monkeypatch, tmp_path, goal="build X", frames=None, marathon=True):
    """Drive AgentSession.run() with everything network-side faked; return the
    JSON body POSTed to /v1/agent/sessions, the run() result, and the sink."""
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST
    import webbee.tools as T

    # Real repo layout so detect_verify_cmd resolves to a known command.
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": str(tmp_path), "git": "", "tree": "", "repo_key": "abc",
        "repo_root": str(tmp_path),
    })

    class _FakeImperalClient:
        def __init__(self, cfg, token_provider): ...
        async def whoami(self): return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    class _RecExecutor:
        def __init__(self, root, indexer=None): ...
        def run(self, tool, args): return {"ok": True, "content": "ran"}

    monkeypatch.setattr(T, "LocalToolExecutor", _RecExecutor)

    # Default protocol (U4 semantics): a marathon_plan FACT + a per-milestone
    # `final` (NON-terminal) + the terminal marathon_complete. Tests override
    # `frames` to exercise the terminal taxonomy.
    _frames = frames if frames is not None else [
        {"type": "marathon_plan", "task_id": "OURS", "milestone_count": 2, "goal": goal},
        {"type": "final", "task_id": "OURS", "text": "milestone 1"},
        {"type": "marathon_complete", "task_id": "OURS", "text": "done"},
    ]

    async def _fake_stream(client, session_id, headers_provider, *, start_id="0-0"):
        for _fr in _frames:
            yield _fr

    monkeypatch.setattr(ST, "stream_frames", _fake_stream)

    posted = {}

    class _SessResp:
        def raise_for_status(self): ...
        def json(self): return {"session_id": "marathon-user-1-rabc", "last_id": "0-0",
                                "task_id": "OURS"}

    class _ResultResp:
        def raise_for_status(self): ...
        def json(self): return {}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, path, headers=None, json=None, **kw):
            if path == "/v1/agent/sessions":
                posted.update(json)
                return _SessResp()
            return _ResultResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    class NoteSink:
        def __init__(self): self.notes = []
        def note(self, msg): self.notes.append(msg)
        def thinking(self, msg): self.thinks = getattr(self, "thinks", []) + [msg]
        def tool_start(self, *a): ...
        def tool_result(self, *a): ...
        def ask_consent(self, *a): return "y"
        def panel_release(self, *a): ...
        def progress(self, *a): ...
        def usage(self, *a): ...

    async def token_provider(): return "tok"

    class _Cfg:
        api_url = "https://api.example"

    sink = NoteSink()
    sess = S.AgentSession(cfg=_Cfg(), token_provider=token_provider, workspace_root=str(tmp_path))
    result = asyncio.run(sess.run(goal, sink, marathon=marathon, goal=(goal if marathon else "")))
    return posted, result, sink


def test_marathon_run_sets_marathon_and_goal_and_verify_cmd(monkeypatch, tmp_path):
    posted, result, sink = _run_marathon_capture_post(monkeypatch, tmp_path, goal="build X")
    assert posted["marathon"] is True
    assert posted["goal"] == "build X"
    # verify_cmd is CLIENT-detected and rides inside coding_context.
    assert posted["coding_context"]["verify_cmd"] == "pytest -q"
    assert result == "done"
    # marathon_plan FACT rendered as a one-line note (did not crash the turn).
    assert any("Marathon plan" in n for n in sink.notes)


def test_marathon_final_is_not_terminal_keeps_streaming(monkeypatch, tmp_path):
    # A per-milestone `final` must NOT end a marathon run: with no terminal frame
    # the stream simply exhausts and run() returns "" (it kept streaming past the
    # milestone final, never bailed early — the phantom-hang root cause).
    _, result, _ = _run_marathon_capture_post(monkeypatch, tmp_path, frames=[
        {"type": "marathon_plan", "task_id": "OURS", "goal": "g"},
        {"type": "final", "task_id": "OURS", "text": "milestone 1 done"},
    ])
    assert result == ""


def test_marathon_complete_is_terminal(monkeypatch, tmp_path):
    _, result, _ = _run_marathon_capture_post(monkeypatch, tmp_path, frames=[
        {"type": "final", "task_id": "OURS", "text": "milestone 1"},
        {"type": "marathon_complete", "task_id": "OURS", "text": "goal done"},
    ])
    assert result == "goal done"


def test_thinking_frame_renders_as_distinct_channel(monkeypatch, tmp_path):
    # A `thinking` frame routes to the distinct 💭 reasoning channel
    # (sink.thinking), NOT the plain progress/note lines.
    _, _, sink = _run_marathon_capture_post(monkeypatch, tmp_path, frames=[
        {"type": "thinking", "task_id": "OURS", "text": "assessing the repo before editing"},
        {"type": "marathon_complete", "task_id": "OURS", "text": "done"},
    ])
    assert getattr(sink, "thinks", []) == ["assessing the repo before editing"]


def test_marathon_paused_is_terminal_and_noted(monkeypatch, tmp_path):
    _, result, sink = _run_marathon_capture_post(monkeypatch, tmp_path, frames=[
        {"type": "marathon_paused", "task_id": "OURS", "reason": "out of credits"},
    ])
    assert result == ""
    assert any("paused" in n.lower() for n in sink.notes)


def test_marathon_user_stop_final_is_terminal(monkeypatch, tmp_path):
    # A user-stop (stopped=true) `final` ends the marathon immediately.
    _, result, _ = _run_marathon_capture_post(monkeypatch, tmp_path, frames=[
        {"type": "final", "task_id": "OURS", "text": "partial", "stopped": True},
    ])
    assert result == "partial"


def test_non_marathon_final_is_terminal(monkeypatch, tmp_path):
    # Coding (non-marathon): a `final` is terminal (unchanged) -> returns its text.
    _, result, _ = _run_marathon_capture_post(monkeypatch, tmp_path, marathon=False, frames=[
        {"type": "final", "task_id": "OURS", "text": "answer"},
    ])
    assert result == "answer"


def test_normal_run_omits_marathon_fields(monkeypatch, tmp_path):
    # Coding path unchanged: no marathon/goal keys, no verify_cmd injected.
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST
    import webbee.tools as T

    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": root, "git": "", "tree": "", "repo_key": "abc", "repo_root": root,
    })

    class _FakeImperalClient:
        def __init__(self, cfg, token_provider): ...
        async def whoami(self): return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    class _RecExecutor:
        def __init__(self, root, indexer=None): ...
        def run(self, tool, args): return {"ok": True, "content": "ran"}

    monkeypatch.setattr(T, "LocalToolExecutor", _RecExecutor)

    async def _fake_stream(client, session_id, headers_provider, *, start_id="0-0"):
        yield {"type": "final", "task_id": "OURS", "text": "done"}

    monkeypatch.setattr(ST, "stream_frames", _fake_stream)

    posted = {}

    class _SessResp:
        def raise_for_status(self): ...
        def json(self): return {"session_id": "coding-user-1-1", "last_id": "0-0",
                                "task_id": "OURS"}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, path, headers=None, json=None, **kw):
            if path == "/v1/agent/sessions":
                posted.update(json)
            return _SessResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    class Sink:
        def tool_start(self, *a): ...
        def tool_result(self, *a): ...
        def ask_consent(self, *a): return "y"
        def panel_release(self, *a): ...
        def progress(self, *a): ...
        def usage(self, *a): ...

    async def token_provider(): return "tok"

    class _Cfg:
        api_url = "https://api.example"

    sess = S.AgentSession(cfg=_Cfg(), token_provider=token_provider, workspace_root=".")
    asyncio.run(sess.run("just code", Sink()))
    assert "marathon" not in posted
    assert "goal" not in posted
    assert "verify_cmd" not in posted["coding_context"]


# --- marathon_note renderer --------------------------------------------------

def test_marathon_note_plan_milestone_pause():
    from webbee.frames import marathon_note
    assert "Marathon plan" in marathon_note(
        {"type": "marathon_plan", "milestone_count": 3, "goal": "ship it"})
    assert "Milestone" in marathon_note(
        {"type": "milestone", "index": 1, "title": "tests green", "status": "done"})
    assert "paused" in marathon_note(
        {"type": "marathon_paused", "reason": "awaiting consent"}).lower()


def test_marathon_note_degrades_on_missing_fields():
    from webbee.frames import marathon_note
    # No fields at all — must not raise; returns a bare label.
    assert marathon_note({"type": "marathon_plan"}) == "🏁 Marathon plan"
    assert marathon_note({"type": "milestone"}) == "• Milestone"
    assert marathon_note({"type": "marathon_paused"}) == "⏸ Marathon paused"


def test_todo_fact_renders_progress_line():
    from webbee.frames import _MARATHON_FACT_TYPES, marathon_note
    assert "todo" in _MARATHON_FACT_TYPES
    note = marathon_note({"type": "todo", "total": 3, "completed": 1, "todos": [
        {"content": "map the repo", "status": "completed"},
        {"content": "fix the bug", "status": "in_progress"},
        {"content": "run tests", "status": "pending"}]})
    assert "1/3" in note and "fix the bug" in note


def test_todo_fact_degrades_on_missing_fields():
    from webbee.frames import marathon_note
    assert "0/0" in marathon_note({"type": "todo"})          # never crashes
