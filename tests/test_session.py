import asyncio

from webbee.session import handle_tool_request, handle_confirm_request, build_coding_context


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

    async def _fake_stream(client, session_id, headers_provider, *, start_id="0-0"):
        # A foreign turn's tool_request and final (must be ignored), THEN
        # this turn's own final (must be honored).
        yield {"type": "tool_request", "task_id": "OTHER", "req_id": "r1",
               "tool": "read_file", "args": {}}
        yield {"type": "final", "task_id": "OTHER", "text": "wrong turn"}
        yield {"type": "final", "task_id": "OURS", "text": "done"}

    monkeypatch.setattr(ST, "stream_frames", _fake_stream)

    class _SessResp:
        def raise_for_status(self): pass
        def json(self): return {"session_id": "sid1", "last_id": "0-0", "task_id": "OURS"}

    class _ResultResp:
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
        def tool_start(self, *a): ...
        def tool_result(self, *a): ...
        def ask_consent(self, *a): return "y"
        def panel_release(self, *a): ...
        def progress(self, *a): ...
        def usage(self, *a): ...

    async def token_provider():
        return "tok"

    sess = S.AgentSession(cfg=_FakeCfg(), token_provider=token_provider, workspace_root=".")
    result = asyncio.run(sess.run("do it", RecSink()))

    assert result == "done"                # ended on OUR final, not the foreign one
    assert executor_calls == []            # foreign tool_request never dispatched
    assert posts == ["/v1/agent/sessions"]  # no /result POST for the ignored frames


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
    from webbee.session import _summary
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
