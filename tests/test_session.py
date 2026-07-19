import asyncio

from webbee.coding_context import build_coding_context
from webbee.consent import handle_confirm_request
from webbee.frames import handle_tool_request


class RecordingExecutor:
    def __init__(self): self.calls = []
    def run(self, tool, args):
        self.calls.append((tool, args))
        return {"ok": True, "content": f"ran {tool}"}


def test_handle_tool_request_runs_and_wraps():
    ex = RecordingExecutor()
    out = handle_tool_request({"req_id": "r1", "tool": "read_file", "args": {"path": "a"}}, ex)
    assert out == {"req_id": "r1", "result": {"ok": True, "content": "ran read_file"}}
    assert ex.calls == [("read_file", {"path": "a"})]


def test_confirm_autopilot_approves_without_asking():
    called = []
    out = asyncio.run(handle_confirm_request(
        {"req_id": "r2"}, "autopilot", lambda a, t, g: called.append(1) or "x"))
    assert out == {"req_id": "r2", "result": {"approved": True}}
    assert called == []  # never prompts in autopilot


def test_confirm_plan_denies_with_reason_without_asking():
    called = []
    out = asyncio.run(handle_confirm_request(
        {"req_id": "r3", "tool": "delete_note"}, "plan",
        lambda a, t, g: called.append(1) or "x"))
    assert out == {"req_id": "r3", "result": {"approved": False, "reason": "plan_mode"}}
    assert called == []  # plan never prompts either


def test_confirm_default_relays_raw_reply_verbatim():
    # ICNLI: client must NOT interpret — it relays the raw reply as-is.
    frame = {"req_id": "r4", "app_id": "webbee", "tool": "bash", "args": {"command": "ls"}}
    seen = {}
    async def ask(app_id, tool, args):
        seen.update(app_id=app_id, tool=tool, args=args)
        return "давай, только осторожно"
    out = asyncio.run(handle_confirm_request(frame, "default", ask))
    assert out == {"req_id": "r4", "result": {"consent_reply": "давай, только осторожно"}}
    assert seen == {"app_id": "webbee", "tool": "bash", "args": {"command": "ls"}}


def test_build_coding_context_shape(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    ctx = build_coding_context(str(tmp_path))
    assert set(ctx) == {"cwd", "git", "tree", "repo_key", "repo_root"}
    assert "a.txt" in ctx["tree"]


def test_coding_context_includes_repo_profile_when_intel_ready(tmp_path):
    import webbee.session as S

    class _Svc:
        ready = True
        def repo_profile(self): return {"file_count": 3, "languages": {"python": 3}}

    ctx = S.build_coding_context(str(tmp_path), intel=_Svc())
    assert ctx["repo_profile"]["file_count"] == 3


def test_coding_context_no_profile_without_intel(tmp_path):
    import webbee.session as S
    ctx = S.build_coding_context(str(tmp_path), intel=None)
    assert "repo_profile" not in ctx


def test_run_offloads_blocking_context_build_off_event_loop(monkeypatch):
    # Regression (freeze bug): build_coding_context does sync subprocess.run(git
    # status, timeout=10) + os.walk. Called inline on the dock's asyncio loop it
    # BLOCKED the whole UI at every turn start (freeze / "не реагирует"). run()
    # must offload it to a worker thread so the event loop stays responsive.
    import threading

    import webbee.session as S

    main = threading.main_thread()
    captured = {}

    class _Sentinel(Exception):
        pass

    def _spy(root, intel=None):
        captured["thread"] = threading.current_thread()
        raise _Sentinel  # short-circuit run() before any network I/O

    monkeypatch.setattr(S, "build_coding_context", _spy)
    sess = S.AgentSession(cfg=object(), token_provider=lambda: None, workspace_root=".")
    try:
        asyncio.run(sess.run("task", sink=None))
    except _Sentinel:
        pass
    assert captured.get("thread") is not None, "build_coding_context was never called"
    assert captured["thread"] is not main, "context build ran ON the event-loop thread (blocks UI)"


def test_run_ignores_foreign_turn_actionable_frames_ends_on_own_final(monkeypatch):
    # C7: the gateway stamps every turn frame with task_id and the CLI POSTs
    # once per turn onto a SHARED persistent stream. A reconnecting client can
    # therefore see a PRIOR turn's frames -- it must ignore their actionable
    # frames (never dispatch a foreign tool_request to the executor) and
    # terminate ONLY on its own final, not a stale one.
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST
    import webbee.tools as T

    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": root, "git": "", "tree": "", "repo_key": "x", "repo_root": root,
    })

    class _FakeImperalClient:
        def __init__(self, cfg, token_provider):
            pass

        async def whoami(self):
            return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    executor_calls = []

    class _RecExecutor:
        def __init__(self, root, indexer=None, shadow=None):
            pass

        def run(self, tool, args):
            executor_calls.append((tool, args))
            return {"ok": True, "content": "ran"}

    monkeypatch.setattr(T, "LocalToolExecutor", _RecExecutor)

    async def _fake_stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        # A foreign turn's tool_request and final (must NEVER be executed /
        # terminal for THIS turn -- only rendered as tagged lines), THEN this
        # turn's own final (must be honored). Cross-surface frames carry the
        # kernel-stamped `origin`; a stale prior-turn frame carries none.
        yield {"type": "tool_request", "task_id": "OTHER", "req_id": "r1",
               "tool": "read_file", "args": {}}
        yield {"type": "tool_request", "task_id": "TG", "req_id": "r2",
               "tool": "write_file", "args": {}, "origin": "telegram"}
        yield {"type": "confirm_request", "task_id": "TG", "req_id": "r3",
               "tool": "bash", "args": {}, "origin": "telegram"}
        yield {"type": "progress", "task_id": "TG", "text": "thinking it over",
               "origin": "telegram"}
        yield {"type": "usage", "task_id": "TG", "tokens": 5, "credits": 1,
               "origin": "telegram"}  # nothing to show -> silently skipped
        yield {"type": "final", "task_id": "TG", "text": "done on telegram",
               "origin": "telegram"}
        yield {"type": "final", "task_id": "OTHER", "text": "wrong turn"}
        yield {"type": "final", "task_id": "OURS", "text": "done"}

    monkeypatch.setattr(ST, "stream_frames", _fake_stream)

    class _SessResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"session_id": "sid1", "last_id": "0-0", "task_id": "OURS"}

    class _ResultResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {}

    posts = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, path, headers=None, **kw):
            posts.append(path)
            return _SessResp() if path == "/v1/agent/sessions" else _ResultResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    class RecSink:
        def __init__(self):
            self.foreign = []; self.progress_calls = []; self.usage_calls = []
        def tool_start(self, *a): ...
        def tool_result(self, *a): ...
        def ask_consent(self, *a): return "y"
        def consent_dismissed(self, note): ...
        def panel_release(self, *a): ...
        def progress(self, text): self.progress_calls.append(text)
        def usage(self, *a): self.usage_calls.append(a)
        def foreign_turn(self, surface, role, text): self.foreign.append((surface, role, text))
        def todos(self, items, total, done): self.todo_lists = getattr(self, "todo_lists", []) + [(items, total, done)]

    async def token_provider():
        return "tok"

    sess = S.AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    sink = RecSink()
    result = asyncio.run(sess.run("do it", sink))

    assert result == "done"                # ended on OUR final, not a foreign one
    assert executor_calls == []            # foreign tool_requests never dispatched
    assert posts == ["/v1/agent/sessions"]  # no /result POST for the foreign frames
    # Cross-surface/stale frames render as DISPLAY-ONLY tagged lines (origin
    # tag; empty for a stale/legacy frame) -- and nothing else happens.
    assert sink.foreign == [
        ("", "assistant", "running read_file"),
        ("telegram", "assistant", "running write_file"),
        ("telegram", "assistant", "approval requested: bash"),
        ("telegram", "assistant", "thinking it over"),
        ("telegram", "assistant", "done on telegram"),
        ("", "assistant", "wrong turn"),
    ]
    assert sink.progress_calls == []       # foreign progress never leaks into the own feed
    assert sink.usage_calls == []          # foreign usage: nothing to show, nothing counted


def test_own_turn_frames_with_cross_surface_origin_render_tagged_and_execute(monkeypatch):
    # Live steer topology (final review C1): a Telegram-steered turn keeps the
    # terminal's OWN task_id (the kernel drain adopts it -- the terminal stays
    # the sole executor) and stamps `origin` with the source surface. The own
    # path must behave EXACTLY as today (execute once, dedup, accounting,
    # consent flow untouched) with the surface tag prefixed onto the text
    # renders; own frames WITHOUT origin render byte-identical to today.
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST
    import webbee.tools as T

    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": root, "git": "", "tree": "", "repo_key": "x", "repo_root": root,
    })

    class _FakeImperalClient:
        def __init__(self, cfg, token_provider):
            pass

        async def whoami(self):
            return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    executor_calls = []

    class _RecExecutor:
        def __init__(self, root, indexer=None, shadow=None):
            pass

        def run(self, tool, args):
            executor_calls.append((tool, args))
            return {"ok": True, "content": "ran"}

    monkeypatch.setattr(T, "LocalToolExecutor", _RecExecutor)

    async def _fake_stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield {"type": "tool_request", "task_id": "OURS", "req_id": "r1",
               "tool": "read_file", "args": {}, "origin": "telegram"}
        yield {"type": "tool_request", "task_id": "OURS", "req_id": "r1",
               "tool": "read_file", "args": {}, "origin": "telegram"}  # dup req_id -> dedup
        yield {"type": "tool_request", "task_id": "OURS", "req_id": "r2",
               "tool": "write_file", "args": {}}                       # no origin -> untagged
        yield {"type": "progress", "task_id": "OURS", "text": "editing files",
               "origin": "telegram"}
        yield {"type": "thinking", "task_id": "OURS", "llm_text": "pondering",
               "origin": "telegram"}
        yield {"type": "progress", "task_id": "OURS", "text": "plain progress"}
        yield {"type": "progress", "task_id": "OURS", "text": "own terminal line",
               "origin": "terminal"}                                    # own surface -> no tag
        yield {"type": "progress", "task_id": "OURS", "text": "",
               "origin": "telegram"}   # empty text -> stays empty, no lone tag line
        yield {"type": "final", "task_id": "OURS", "text": "steered done",
               "origin": "telegram"}

    monkeypatch.setattr(ST, "stream_frames", _fake_stream)

    class _SessResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"session_id": "sid1", "last_id": "0-0", "task_id": "OURS"}

    class _ResultResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {}

    posts = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, path, headers=None, **kw):
            posts.append(path)
            return _SessResp() if path == "/v1/agent/sessions" else _ResultResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    class RecSink:
        def __init__(self):
            self.starts = []; self.results = []; self.progress_calls = []; self.foreign = []
        def tool_start(self, tool, args): self.starts.append((tool, dict(args)))
        def tool_result(self, tool, ok, summary): self.results.append((tool, ok, summary))
        def ask_consent(self, *a): return "y"
        def consent_dismissed(self, note): ...
        def panel_release(self, *a): ...
        def progress(self, text): self.progress_calls.append(text)
        def usage(self, *a): ...
        def foreign_turn(self, surface, role, text): self.foreign.append((surface, role, text))
        def todos(self, items, total, done): self.todo_lists = getattr(self, "todo_lists", []) + [(items, total, done)]

    async def token_provider():
        return "tok"

    sess = S.AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    sink = RecSink()
    result = asyncio.run(sess.run("do it", sink))

    assert result == "[telegram] steered done"      # final text carries the tag
    # Execution/dedup/accounting IDENTICAL to today: each req_id runs once,
    # the dup re-posts the cached result (at-least-once), steps record cleanly.
    assert executor_calls == [("read_file", {}), ("write_file", {})]
    assert posts[0] == "/v1/agent/sessions"
    assert posts.count("/v1/agent/sessions/sid1/result") == 3  # r1, r1-dup, r2
    assert sess.steps == [
        {"step_id": "r1", "label": "read_file", "ok": True},
        {"step_id": "r2", "label": "write_file", "ok": True},
    ]
    # Tool lines: tagged when steered, untagged otherwise.
    assert sink.starts == [("[telegram] read_file", {}), ("write_file", {})]
    assert sink.results == [("[telegram] read_file", True, "ran"),
                            ("write_file", True, "ran")]
    # Text lines: tagged when steered; no stray tag for no-origin/terminal.
    assert sink.progress_calls == ["[telegram] editing files", "[telegram] pondering",
                                   "plain progress", "own terminal line", ""]
    assert sink.foreign == []                        # own-path never routes via foreign_turn


def test_origin_tag_prefix_only_for_other_surfaces():
    from webbee.frames import _origin_tag
    assert _origin_tag({"origin": "telegram"}) == "[telegram] "
    assert _origin_tag({"origin": "web-panel"}) == "[web-panel] "
    assert _origin_tag({"origin": "terminal"}) == ""   # own surface
    assert _origin_tag({"origin": ""}) == ""
    assert _origin_tag({}) == ""


def test_action_frame_maps_to_feed():
    class Rec:
        def __init__(self): self.starts=[]; self.results=[]
        def tool_start(self, label, args): self.starts.append(label)
        def tool_result(self, tool, ok, summary): self.results.append((ok, summary))
        def ask_consent(self,*a): return "y"
        def panel_release(self,*a): ...
        def progress(self,*a): ...
        def usage(self,*a): ...
    r = Rec()
    # emulate the session's action-branch dispatch contract:
    r.tool_start("tasks·list_tasks", {}); r.tool_result("tasks·list_tasks", True, "200 open")
    assert r.starts == ["tasks·list_tasks"] and r.results == [(True, "200 open")]


def test_usage_frame_forwards_tokens_and_cost():
    from webbee.session import AgentSession  # noqa: F401 — import-time sanity check
    # The session's frame dispatch is exercised end-to-end in the live smoke;
    # here we assert the sink contract shape the session will call.
    class Rec:
        def __init__(self): self.calls = []
        def tool_start(self, *a): ...
        def tool_result(self, *a): ...
        def ask_consent(self, *a): return "y"
        def panel_release(self, *a): ...
        def progress(self, text): self.calls.append(("progress", text))
        def usage(self, tokens, cost_usd): self.calls.append(("usage", tokens, cost_usd))
    r = Rec()
    r.usage(1234, 0.05); r.progress("reading files")
    assert ("usage", 1234, 0.05) in r.calls and ("progress", "reading files") in r.calls


# --- Slice-5 T9: frame v2 (step_started/step_finished) -----------------------
# The kernel (Slice-5 T8) dual-emits step_started/step_finished ALONGSIDE the
# legacy action start/done and local tool_request frames during the compat
# window. The CLI must accept both vocabularies and render IDENTICAL dock
# output either way -- no double-counted "N actions", no doubled result line.


class RecSink:
    def __init__(self):
        self.starts = []
        self.results = []

    def tool_start(self, tool, args):
        self.starts.append((tool, dict(args)))

    def tool_result(self, tool, ok, summary):
        self.results.append((tool, ok, summary))

    def todos(self, items, total, done):
        self.todo_lists = getattr(self, "todo_lists", []) + [(items, total, done)]

    def consent_dismissed(self, note): ...


def test_v2_step_label_matches_old_action_label_ladder():
    from webbee.frames import _v2_step_label
    assert _v2_step_label({"app_id": "mail", "tool": "list"}) == "mail·list"
    # local tools carry no app_id -- degrade gracefully to the bare tool name.
    assert _v2_step_label({"tool": "read_file"}) == "read_file"
    assert _v2_step_label({}) == ""


def test_summary_from_facts_degrades_gracefully_when_entity_kind_is_empty():
    from webbee.frames import _summary_from_facts
    # T8 flagged entity_kind empty at today's kernel call sites -- must not crash
    # or print an ugly "None"; degrade to a bare count.
    assert _summary_from_facts({"ok": True, "count": 3}) == "3"
    assert _summary_from_facts({}) == ""
    assert _summary_from_facts({"count": 0}) == "0"


def test_summary_from_facts_pluralizes_with_entity_kind_when_present():
    from webbee.frames import _summary_from_facts
    assert _summary_from_facts({"count": 1, "entity_kind": "message"}) == "1 message"
    assert _summary_from_facts({"count": 3, "entity_kind": "message"}) == "3 messages"


def test_render_foreign_frame_is_display_only_and_never_raises():
    # Cross-surface frames (Telegram/panel steering) get ONE tagged line and
    # NOTHING else. The render side effect must never break the safety
    # `continue` in session.run(): a sink without foreign_turn drops the line;
    # a sink whose foreign_turn raises is swallowed.
    from webbee.frames import render_foreign_frame

    class Rec:
        def __init__(self): self.calls = []
        def foreign_turn(self, surface, role, text): self.calls.append((surface, role, text))

    r = Rec()
    render_foreign_frame({"type": "tool_request", "tool": "bash", "origin": "web-panel"}, r)
    assert r.calls == [("web-panel", "assistant", "running bash")]
    render_foreign_frame({"type": "usage", "tokens": 1, "origin": "telegram"}, r)
    assert len(r.calls) == 1               # nothing meaningful to show -> skipped
    render_foreign_frame({"type": "final", "text": "", "origin": "telegram"}, r)
    assert len(r.calls) == 1               # empty final text -> skipped
    render_foreign_frame({"type": "progress", "llm_text": "planning", "origin": "telegram"}, r)
    assert r.calls[-1] == ("telegram", "assistant", "planning")

    class Bare:                            # minimal sink (no foreign_turn) -> no-op
        pass
    render_foreign_frame({"type": "final", "text": "x", "origin": "telegram"}, Bare())

    class Boom:
        def foreign_turn(self, *a): raise RuntimeError("ui bug")
    render_foreign_frame({"type": "final", "text": "x", "origin": "telegram"}, Boom())


def test_handle_step_started_calls_tool_start_once():
    from webbee.frames import handle_step_started
    sink = RecSink()
    started, labels, local_ids = set(), {}, set()
    handle_step_started({"type": "step_started", "step_id": "s1", "kind": "ext_tool",
                          "app_id": "mail", "tool": "list"}, sink, started, labels, local_ids)
    assert sink.starts == [("mail·list", {})]
    assert "s1" in started and labels["s1"] == "mail·list"


def test_handle_step_started_dedups_same_step_id():
    from webbee.frames import handle_step_started
    sink = RecSink()
    started, labels, local_ids = set(), {}, set()
    frame = {"type": "step_started", "step_id": "s1", "kind": "tool", "tool": "list"}
    handle_step_started(frame, sink, started, labels, local_ids)
    handle_step_started(frame, sink, started, labels, local_ids)
    assert len(sink.starts) == 1  # NOT double-counted (would inflate "N actions")


def test_handle_step_finished_calls_tool_result_and_appends_step():
    from webbee.frames import handle_step_started, handle_step_finished
    sink = RecSink()
    started, finished, labels, steps, local_ids = set(), set(), {}, [], set()
    handle_step_started({"step_id": "s1", "app_id": "mail", "tool": "list"}, sink, started, labels, local_ids)
    handle_step_finished({"step_id": "s1", "ok": True, "duration_ms": 250,
                          "summary_facts": {"ok": True, "count": 2, "entity_kind": "message"}},
                         sink, finished, labels, steps, local_ids)
    assert sink.results == [("mail·list", True, "2 messages")]
    assert steps == [{"step_id": "s1", "label": "mail·list", "ok": True}]


def test_handle_step_finished_dedups_same_step_id():
    from webbee.frames import handle_step_finished
    sink = RecSink()
    finished, labels, steps, local_ids = set(), {}, [], set()
    frame = {"step_id": "s1", "ok": True, "duration_ms": 10, "summary_facts": {}}
    handle_step_finished(frame, sink, finished, labels, steps, local_ids)
    handle_step_finished(frame, sink, finished, labels, steps, local_ids)
    assert len(sink.results) == 1  # NOT double-printed
    assert len(steps) == 1


def test_handle_step_started_local_tool_is_a_noop():
    # GROUND TRUTH (coding_agent_workflow._dispatch_local_raw): local tools
    # get a SEPARATE, server-generated req_id ("req-{session}-{n}") that
    # never equals step_id (tc["id"]) -- the two vocabularies can't be
    # id-deduped for local tools, so step_started/step_finished must be a
    # pure no-op there and let the (unchanged) tool_request/result round
    # trip render the step alone.
    from webbee.frames import handle_step_started
    sink = RecSink()
    started, labels, local_ids = set(), {}, set()
    handle_step_started({"step_id": "toolu_1", "kind": "local_tool", "tool": "read_file"},
                        sink, started, labels, local_ids)
    assert sink.starts == []
    assert started == set()
    assert local_ids == {"toolu_1"}


def test_handle_step_finished_local_tool_is_a_noop_and_clears_local_ids():
    from webbee.frames import handle_step_started, handle_step_finished
    sink = RecSink()
    started, finished, labels, steps, local_ids = set(), set(), {}, [], set()
    handle_step_started({"step_id": "toolu_1", "kind": "local_tool", "tool": "read_file"},
                        sink, started, labels, local_ids)
    handle_step_finished({"step_id": "toolu_1", "ok": True, "duration_ms": 5, "summary_facts": {}},
                         sink, finished, labels, steps, local_ids)
    assert sink.results == []
    assert steps == []
    assert local_ids == set()  # cleaned up, doesn't leak across turns


def test_local_tool_v2_frames_dont_double_the_tool_request_render():
    # End-to-end regression for the id-mismatch: simulate the REAL frame
    # order for one local tool call under v2 dual-emit -- step_started,
    # then the (unrelated req_id) tool_request round trip renders via the
    # EXISTING mechanism, then step_finished. Only ONE start/result pair
    # must reach the sink.
    from webbee.frames import handle_step_started, handle_step_finished, _first_time
    from webbee.frames import _summary
    sink = RecSink()
    started, finished, labels, steps, local_ids = set(), set(), {}, [], set()
    handle_step_started({"step_id": "toolu_1", "kind": "local_tool", "tool": "read_file"},
                        sink, started, labels, local_ids)
    # tool_request path (mirrors run()'s inline branch), a DIFFERENT id:
    req_sid = "req-coding-xyz-0"
    if _first_time(req_sid, started):
        sink.tool_start("read_file", {"path": "src/main.py"})
    res = {"ok": True, "content": "file contents"}
    if _first_time(req_sid, finished):
        sink.tool_result("read_file", bool(res.get("ok")), _summary(res))
        steps.append({"step_id": req_sid, "label": "read_file", "ok": True})
    handle_step_finished({"step_id": "toolu_1", "ok": True, "duration_ms": 12, "summary_facts": {}},
                         sink, finished, labels, steps, local_ids)
    assert len(sink.starts) == 1
    assert len(sink.results) == 1
    assert len(steps) == 1


def test_handle_action_frame_start_and_done_unchanged_shape():
    from webbee.frames import handle_action_frame
    sink = RecSink()
    started, finished, steps = set(), set(), []
    handle_action_frame({"phase": "start", "step_id": "a1", "app_id": "tasks", "tool": "list_tasks"},
                        sink, started, finished, steps)
    handle_action_frame({"phase": "done", "step_id": "a1", "app_id": "tasks", "tool": "list_tasks",
                        "ok": True, "summary": "200 open"}, sink, started, finished, steps)
    assert sink.starts == [("tasks·list_tasks", {})]
    assert sink.results == [("tasks·list_tasks", True, "200 open")]
    assert steps == [{"step_id": "a1", "label": "tasks·list_tasks", "ok": True}]


def test_cross_vocab_dedup_step_started_then_action_start_same_id():
    # Dual-emit ordering: v2 step_started fires, THEN (same logical step) an
    # old-vocab action-start frame for the SAME step_id arrives -- must not
    # double sink.tool_start (would double-count "N actions" in the toolbar).
    from webbee.frames import handle_step_started, handle_action_frame
    sink = RecSink()
    started, finished, labels, steps, local_ids = set(), set(), {}, [], set()
    handle_step_started({"step_id": "a1", "app_id": "tasks", "tool": "list_tasks"}, sink, started, labels, local_ids)
    handle_action_frame({"phase": "start", "step_id": "a1", "app_id": "tasks", "tool": "list_tasks"},
                        sink, started, finished, steps)
    assert len(sink.starts) == 1


def test_cross_vocab_dedup_action_done_then_step_finished_same_id():
    from webbee.frames import handle_action_frame, handle_step_finished
    sink = RecSink()
    started, finished, labels, steps, local_ids = set(), set(), {}, [], set()
    handle_action_frame({"phase": "start", "step_id": "a1", "app_id": "tasks", "tool": "list_tasks"},
                        sink, started, finished, steps)
    handle_action_frame({"phase": "done", "step_id": "a1", "app_id": "tasks", "tool": "list_tasks",
                        "ok": True, "summary": "200 open"}, sink, started, finished, steps)
    handle_step_finished({"step_id": "a1", "ok": True, "duration_ms": 10, "summary_facts": {}},
                         sink, finished, labels, steps, local_ids)
    assert len(sink.results) == 1  # the v2 twin is a no-op once the old vocab already finished it
    assert len(steps) == 1


def test_progress_dual_reads_llm_text_over_legacy_text():
    from webbee.frames import _progress_text
    assert _progress_text({"text": "old", "llm_text": "new"}) == "new"
    assert _progress_text({"text": "legacy-only"}) == "legacy-only"
    assert _progress_text({}) == ""


# --- Liveness A: the consent prompt races the stream --------------------------
# LIVE BUG (Valentin, 2026-07-15): a consent answered from Telegram resolves
# the kernel park, but the frame loop stayed blocked INLINE in
# handle_confirm_request -- the dock froze on `approve? y/n` and the rest of
# the turn never rendered until a local keypress. The local prompt now RACES
# the stream; the ONLY new behavior is during a pending consent.


class ConsentRaceSink:
    """Sink double whose ask_consent mirrors the dock's pinned prompt: it
    blocks on a Future-like wait (forever, or until `release` fires) and
    records whether the race cancelled it."""

    def __init__(self, reply=None, release=None):
        self.consent_calls = []
        self.dismissed = []
        self.thinks = []
        self.progress_calls = []
        self.consent_cancelled = False
        self._reply = reply        # None -> never answered locally
        self._release = release    # optional Event gating the local answer

    async def ask_consent(self, app_id, tool, args):
        self.consent_calls.append((app_id, tool, args))
        try:
            if self._release is not None:
                await self._release.wait()
            elif self._reply is None:
                await asyncio.Event().wait()   # pends forever (no local keypress)
        except asyncio.CancelledError:
            self.consent_cancelled = True
            raise
        return self._reply

    def consent_dismissed(self, note):
        self.dismissed.append(note)

    def tool_start(self, *a): ...
    def tool_result(self, *a): ...
    def panel_release(self, *a): ...
    def progress(self, text): self.progress_calls.append(text)
    def thinking(self, text): self.thinks.append(text)
    def usage(self, *a): ...
    def foreign_turn(self, surface, role, text):
        self.foreign = getattr(self, "foreign", []) + [(surface, role, text)]
    def todos(self, items, total, done):
        self.todo_lists = getattr(self, "todo_lists", []) + [(items, total, done)]
    def plan_blocked(self, *a): ...


def _run_consent_race(monkeypatch, fake_stream, sink, on_result_post=None):
    """Drive AgentSession.run() with the network faked (same style as the C7
    tests above); returns (result, result_posts) where result_posts is every
    JSON body POSTed to the /result endpoint."""
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST
    import webbee.tools as T

    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": root, "git": "", "tree": "", "repo_key": "x", "repo_root": root,
    })

    class _FakeImperalClient:
        def __init__(self, cfg, token_provider): ...
        async def whoami(self): return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    class _NoExecutor:
        def __init__(self, root, indexer=None, shadow=None): ...
        def run(self, tool, args): return {"ok": True, "content": "ran"}

    monkeypatch.setattr(T, "LocalToolExecutor", _NoExecutor)
    monkeypatch.setattr(ST, "stream_frames", fake_stream)

    result_posts = []

    class _SessResp:
        status_code = 200
        def raise_for_status(self): ...
        def json(self): return {"session_id": "sid1", "last_id": "0-0", "task_id": "OURS"}

    class _ResultResp:
        status_code = 200
        def raise_for_status(self): ...
        def json(self): return {}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, path, headers=None, json=None, **kw):
            if path == "/v1/agent/sessions":
                return _SessResp()
            result_posts.append(json)
            if on_result_post is not None:
                on_result_post(json)
            return _ResultResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async def token_provider(): return "tok"

    sess = S.AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    result = asyncio.run(sess.run("do it", sink))
    return result, result_posts


def test_consent_prompt_dismissed_when_turn_continues_from_another_surface(monkeypatch):
    # Test 1: confirm_request parks the turn; a `thinking` frame arrives (the
    # consent was answered from Telegram -- the kernel moved on). The local
    # prompt task must be CANCELLED, the sink told via consent_dismissed, NO
    # consent result POSTed (the kernel accepts only the FIRST result per
    # issued req_id), the thinking frame must render, and the turn must
    # continue to its final normally.
    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield {"type": "confirm_request", "task_id": "OURS", "req_id": "c1",
               "app_id": "webbee", "tool": "bash", "args": {"command": "rm x"}}
        yield {"type": "thinking", "task_id": "OURS", "llm_text": "ok, proceeding"}
        yield {"type": "final", "task_id": "OURS", "text": "all done"}

    sink = ConsentRaceSink()               # never answered locally
    result, posts = _run_consent_race(monkeypatch, _stream, sink)

    assert result == "all done"                       # turn reached its final
    assert sink.consent_cancelled is True             # prompt task cancelled
    assert sink.dismissed == ["↩ answered from another surface"]
    assert posts == []                                # NO consent result POSTed
    assert sink.thinks == ["ok, proceeding"]          # pulled-ahead frame rendered


def test_consent_local_answer_posts_exactly_as_today(monkeypatch):
    # Test 2: the user answers locally FIRST -- the result is POSTed exactly
    # as today, nothing is dismissed/cancelled, and the turn ends normally.
    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield {"type": "confirm_request", "task_id": "OURS", "req_id": "c1",
               "app_id": "webbee", "tool": "bash", "args": {"command": "ls"}}
        yield {"type": "final", "task_id": "OURS", "text": "done"}

    sink = ConsentRaceSink(reply="да, давай")
    result, posts = _run_consent_race(monkeypatch, _stream, sink)

    assert result == "done"
    assert posts == [{"req_id": "c1", "result": {"consent_reply": "да, давай"}}]
    assert sink.consent_cancelled is False
    assert sink.dismissed == []


def test_consent_republished_same_req_id_keeps_prompt_one_post(monkeypatch):
    # Test 3: the kernel RE-PUBLISHES the pending confirm_request with the
    # SAME req_id on a presence flip (I-MARATHON-USER-WAKE). That is NOT an
    # answer -- the local prompt must stay up (not cancelled, not re-prompted);
    # the local answer then produces exactly ONE POST.
    release = asyncio.Event()    # gates the local answer
    answered = asyncio.Event()   # gates the stream's final

    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield {"type": "confirm_request", "task_id": "OURS", "req_id": "c1",
               "tool": "bash", "args": {}}
        yield {"type": "confirm_request", "task_id": "OURS", "req_id": "c1",
               "tool": "bash", "args": {}}    # kernel re-publish, SAME req_id
        release.set()             # both confirms delivered -> NOW answer locally
        await answered.wait()     # hold the stream until the answer is posted
        yield {"type": "final", "task_id": "OURS", "text": "done"}

    sink = ConsentRaceSink(reply="y", release=release)
    result, posts = _run_consent_race(
        monkeypatch, _stream, sink, on_result_post=lambda body: answered.set())

    assert result == "done"
    assert len(sink.consent_calls) == 1               # ONE prompt, never re-prompted
    assert sink.consent_cancelled is False            # re-publish did NOT cancel it
    assert sink.dismissed == []
    assert posts == [{"req_id": "c1", "result": {"consent_reply": "y"}}]


def test_stream_end_while_consent_pending_returns_cleanly(monkeypatch):
    # Test 4: the stream generator ends while the consent is still pending --
    # no hang, no unposted-result crash: the prompt task is cancelled safely
    # and run() exits exactly as today's stream end does (returns "").
    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield {"type": "confirm_request", "task_id": "OURS", "req_id": "c1",
               "tool": "bash", "args": {}}
        # generator ends -> StopAsyncIteration mid-consent-wait

    sink = ConsentRaceSink()               # never answered locally
    result, posts = _run_consent_race(monkeypatch, _stream, sink)

    assert result == ""                    # today's stream-end return
    assert sink.consent_cancelled is True  # prompt retired, no leaked task
    assert posts == []                     # nothing bogus posted
    assert sink.dismissed == []            # nothing was answered anywhere


# --- P5g: AgentSession.stop() — Esc/Ctrl-C server-side cancel -----------------
# Previously Ctrl-C only cancelled the LOCAL asyncio task; the cloud brain kept
# running the turn server-side. stop() posts a cancel for the in-flight
# session so the kernel actually stops, and must fail soft (a network error
# here must never raise into the dock's key-binding handler).

class _FakeCfg:
    api_url = "https://api.example"


class _FakeResponse:
    def raise_for_status(self):
        pass


def test_stop_posts_cancel_to_session_endpoint(monkeypatch):
    import httpx

    from webbee.session import AgentSession

    posts = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, path, headers=None, **kw):
            posts.append((path, headers))
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async def token_provider():
        return "tok"

    sess = AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    sess.session_id = "coding-u-1"
    asyncio.run(sess.stop())

    assert len(posts) == 1
    path, headers = posts[0]
    assert path == "/v1/agent/sessions/coding-u-1/cancel"
    assert headers == {"Authorization": "Bearer tok"}


def test_stop_is_a_noop_without_a_session_id(monkeypatch):
    import httpx

    from webbee.session import AgentSession

    called = []

    class BoomAsyncClient:
        def __init__(self, *a, **kw):
            called.append(1)

    monkeypatch.setattr(httpx, "AsyncClient", BoomAsyncClient)

    async def token_provider():
        return "tok"

    sess = AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    asyncio.run(sess.stop())  # session_id is "" — no request should be attempted
    assert called == []


def test_stop_swallows_network_errors(monkeypatch):
    import httpx

    from webbee.session import AgentSession

    class ExplodingAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", ExplodingAsyncClient)

    async def token_provider():
        return "tok"

    sess = AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    sess.session_id = "coding-u-1"
    asyncio.run(sess.stop())  # must not raise — fail-soft


# ── surface threading into the turn POST (liveness v2 §B) ─────────────────────
# An idle-steer pickup submits the remote instruction through the SAME turn
# path a typed line takes, plus ONE additive body key: `surface` = the queued
# item's origin. The kernel adopts it start-path to stamp provenance/tags.

def _run_turn_capture_body(monkeypatch, **run_kw):
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST

    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": root, "git": "", "tree": "", "repo_key": "x", "repo_root": root,
    })

    class _FakeImperalClient:
        def __init__(self, cfg, token_provider):
            pass

        async def whoami(self):
            return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    async def _fake_stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield {"type": "final", "task_id": "OURS", "text": "done"}

    monkeypatch.setattr(ST, "stream_frames", _fake_stream)

    bodies = []

    class _SessResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"session_id": "sid1", "last_id": "0-0", "task_id": "OURS"}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, path, headers=None, json=None, **kw):
            if path == "/v1/agent/sessions":
                bodies.append(json)
            return _SessResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    class _Sink:
        def progress(self, *a): ...

    async def token_provider():
        return "tok"

    sess = S.AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    out = asyncio.run(sess.run("do it", _Sink(), **run_kw))
    assert out == "done"
    return bodies[0]


def test_run_threads_surface_into_session_post_body(monkeypatch):
    body = _run_turn_capture_body(monkeypatch, surface="telegram")
    assert body["surface"] == "telegram"
    assert body["task"] == "do it"


def test_run_omits_surface_key_for_plain_typed_turns(monkeypatch):
    # Additive-only contract: a normal typed turn's POST body is byte-identical
    # to before -- no `surface` key at all.
    body = _run_turn_capture_body(monkeypatch)
    assert "surface" not in body


def test_run_threads_steer_iid_into_session_post_body(monkeypatch):
    # steer-iid-dedup pickup path: a picked-up remote instruction carries the
    # queue entry's dedup id so the kernel ring can drop an at-least-once twin.
    body = _run_turn_capture_body(monkeypatch, surface="telegram", steer_iid="iid-42")
    assert body["steer_iid"] == "iid-42"
    assert body["surface"] == "telegram"


def test_run_omits_steer_iid_key_for_plain_typed_turns(monkeypatch):
    # A typed turn has no dedup id -- key omitted, body byte-identical to today.
    body = _run_turn_capture_body(monkeypatch)
    assert "steer_iid" not in body


def test_run_omits_steer_iid_key_when_pickup_item_had_none(monkeypatch):
    # Older gateway: /pending-steer items without `iid` -> "" -> key omitted.
    body = _run_turn_capture_body(monkeypatch, surface="telegram", steer_iid="")
    assert "steer_iid" not in body
    assert body["surface"] == "telegram"


# ── 0.3.14: cross-surface queued visibility (task_queued / task_dequeued) ─────
# Full-queue-layer K1: the kernel announces a follow-up queued into the RUNNING
# session (task_queued{origin, steer_iid, text, queue_depth}) and its drain
# (task_dequeued{origin, steer_iid}) on the SAME stream. NO task_id on either
# frame (they belong to the session, not a turn), so the C7 foreign filter
# never eats them; the client routes them to two getattr-guarded DISPLAY-ONLY
# sink hooks that feed the live queue panel. 0.3.15: terminal-origin frames
# route TOO — an Enter-while-busy line is injected into the kernel (never held
# in the local panel), so the kernel echo is its ONLY panel row.

def _run_queue_frames_stream(monkeypatch, frames, sink):
    import httpx
    import imperal_mcp.client as ic
    import webbee.session as S
    import webbee.stream as ST
    import webbee.tools as T

    monkeypatch.setattr(S, "build_coding_context", lambda root, intel=None: {
        "cwd": root, "git": "", "tree": "", "repo_key": "x", "repo_root": root,
    })

    class _FakeImperalClient:
        def __init__(self, cfg, token_provider): pass
        async def whoami(self): return "user-1"

    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)

    class _NoExecutor:
        def __init__(self, root, indexer=None, shadow=None): pass
        def run(self, tool, args): raise AssertionError("no tools in this test")

    monkeypatch.setattr(T, "LocalToolExecutor", _NoExecutor)

    async def _fake_stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        for f in frames:
            yield f

    monkeypatch.setattr(ST, "stream_frames", _fake_stream)

    class _SessResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"session_id": "sid1", "last_id": "0-0", "task_id": "OURS"}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, path, headers=None, **kw): return _SessResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async def token_provider():
        return "tok"

    sess = S.AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    return asyncio.run(sess.run("do it", sink))


def test_task_queued_and_dequeued_route_to_sink_hooks_including_terminal(monkeypatch):
    # 0.3.15 (mid-turn inject): the old terminal-origin skip is GONE — an
    # injected Enter-while-busy line never sits in the local panel, so the
    # kernel's task_queued{origin:terminal} echo is its ONLY row and its
    # task_dequeued clears it when the running turn absorbs it.
    class _QueueSink(RecSink):
        def __init__(self):
            super().__init__()
            self.queued = []; self.dequeued = []
            self.progress_calls = []; self.foreign = []
        def remote_queued(self, origin, text, iid): self.queued.append((origin, text, iid))
        def remote_dequeued(self, origin, iid): self.dequeued.append((origin, iid))
        def progress(self, text): self.progress_calls.append(text)
        def foreign_turn(self, surface, role, text): self.foreign.append((surface, role, text))

    sink = _QueueSink()
    result = _run_queue_frames_stream(monkeypatch, [
        # no task_id on queue frames — never foreign, own elif branch handles them
        {"type": "task_queued", "origin": "telegram", "steer_iid": "i1",
         "text": "fix the tests", "queue_depth": 1},
        {"type": "task_queued", "origin": "terminal", "steer_iid": "i2",
         "text": "own follow-up", "queue_depth": 2},   # injected line's ONLY row
        {"type": "task_dequeued", "origin": "telegram", "steer_iid": "i1"},
        {"type": "task_dequeued", "origin": "terminal", "steer_iid": "i2"},
        {"type": "final", "task_id": "OURS", "text": "done"},
    ], sink)
    assert result == "done"
    assert sink.queued == [("telegram", "fix the tests", "i1"),
                           ("terminal", "own follow-up", "i2")]
    assert sink.dequeued == [("telegram", "i1"), ("terminal", "i2")]
    # display-only: nothing leaked into the turn's other render paths
    assert sink.progress_calls == [] and sink.foreign == []


def test_task_queue_frames_ignored_by_minimal_or_crashing_sink(monkeypatch):
    # Backward/limp-mode safety: a sink WITHOUT the hooks (older embedder,
    # minimal test sink) silently drops the frames, and a hook that CRASHES
    # never breaks the frame loop — the turn still ends on its own final.
    class _CrashSink(RecSink):
        def remote_queued(self, origin, text, iid): raise RuntimeError("ui bug")

    frames = [
        {"type": "task_queued", "origin": "telegram", "steer_iid": "i1",
         "text": "fix", "queue_depth": 1},
        {"type": "task_dequeued", "origin": "telegram", "steer_iid": "i1"},
        {"type": "final", "task_id": "OURS", "text": "done"},
    ]
    assert _run_queue_frames_stream(monkeypatch, frames, RecSink()) == "done"
    assert _run_queue_frames_stream(monkeypatch, frames, _CrashSink()) == "done"


def test_transient_retry_survives_502_then_succeeds():
    import asyncio
    from webbee.session import _transient_retry

    class _R:
        def __init__(self, s):
            self.status_code = s

    seq = [_R(502), _R(503), _R(200)]

    async def send():
        return seq.pop(0)

    r = asyncio.run(_transient_retry(send, attempts=5, base=0.0, cap=0.0))
    assert r.status_code == 200


def test_transient_retry_verdict_passes_through_immediately():
    import asyncio
    from webbee.session import _transient_retry

    class _R:
        status_code = 401

    calls = []

    async def send():
        calls.append(1)
        return _R()

    r = asyncio.run(_transient_retry(send, attempts=5, base=0.0, cap=0.0))
    assert r.status_code == 401 and len(calls) == 1   # a verdict is NOT retried


def test_post_result_retries_then_gives_up_silently():
    import asyncio
    from webbee.session import AgentSession

    class _Client:
        def __init__(self):
            self.calls = 0

        async def post(self, url, json=None, headers=None):
            self.calls += 1
            raise OSError("down")

    s = AgentSession.__new__(AgentSession)
    s.token_provider = None

    async def _h():
        return {}

    s._headers = _h
    s._result_delays = (0.0, 0.0, 0.0)   # test seam: no real sleeps
    c = _Client()
    asyncio.run(s._post_result(c, "sid", {"req_id": "r1"}))   # must NOT raise
    assert c.calls == 4   # 1 try + 3 retries
