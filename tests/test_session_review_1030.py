"""2026-10-30 review fixes on the session side of the reverse channel:
H5 (a result is delivered only on a 2xx; 401 refresh, a refusal note, a size
cap, the user told), H6 (an executed request is recorded on disk and never
run twice), H3 (Esc/stop kill the command a cancelled tool still runs), M7 (a
cleanly closed stream backs off; kernel silence is told, then ends the turn)
and M3 (plan mode refuses write-tier tools locally)."""
import asyncio
import json
import threading

import pytest

import webbee.session as S


class _Sink:
    def __init__(self):
        self.notes, self.starts, self.results, self.blocked = [], [], [], []

    def note(self, text): self.notes.append(text)
    def tool_start(self, tool, args): self.starts.append(tool)
    def tool_result(self, tool, ok, summary): self.results.append((tool, ok))
    def plan_blocked(self, tool): self.blocked.append(tool)
    def progress(self, text): ...


class _Resp:
    def __init__(self, status, reason=""):
        self.status_code = status
        self.reason_phrase = reason


class _Client:
    """Answers result POSTs from a status script (the last status repeats)."""
    def __init__(self, statuses=(200,)):
        self.statuses = list(statuses)
        self.posts = []

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json, dict(headers or {})))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return _Resp(status, {413: "Payload Too Large", 422: "Unprocessable Entity",
                              401: "Unauthorized"}.get(status, ""))


def _bare_session(token="tok"):
    s = S.AgentSession(cfg=None, token_provider=None, workspace_root=".")
    tokens = {"t": token}

    async def token_provider():
        return tokens["t"]

    async def force_refresh():
        tokens["t"] = "fresh"
        token_provider.refreshed = True
        return "fresh"

    token_provider.force_refresh = force_refresh
    token_provider.refreshed = False
    s.token_provider = token_provider
    s._result_delays = (0.0, 0.0, 0.0)
    return s


# ── H5: delivery ─────────────────────────────────────────────────────────────

def test_413_posts_a_small_failure_note_under_the_same_req_id_and_tells_the_user():
    s, c, sink = _bare_session(), _Client([413, 200]), _Sink()
    out = {"req_id": "r1", "result": {"ok": True, "content": "x" * 1000}}
    assert asyncio.run(s._post_result(c, "sid", out, sink=sink, tool="view_image")) is False
    assert len(c.posts) == 2                              # the 413 is NOT retried as is
    note = c.posts[1][1]
    assert note["req_id"] == "r1" and note["result"]["ok"] is False
    assert note["result"]["content"].startswith("result could not be delivered: 413 Payload Too Large, ")
    assert "bytes" in note["result"]["content"]
    assert any("view_image" in n and "413" in n for n in sink.notes)


def test_422_gets_the_same_failure_note():
    s, c = _bare_session(), _Client([422, 200])
    asyncio.run(s._post_result(c, "sid", {"req_id": "r2", "result": {"ok": True, "content": "x"}}))
    assert c.posts[1][1]["result"]["content"].startswith("result could not be delivered: 422")


def test_401_refreshes_the_token_once_and_delivers():
    s, c, sink = _bare_session(), _Client([401, 200]), _Sink()
    out = {"req_id": "r1", "result": {"ok": True, "content": "done"}}
    assert asyncio.run(s._post_result(c, "sid", out, sink=sink)) is True
    assert s.token_provider.refreshed
    assert [p[1] for p in c.posts] == [out, out]           # the real result, twice -- no failure note
    assert c.posts[1][2]["Authorization"] == "Bearer fresh"
    assert sink.notes == []


def test_2xx_on_first_try_posts_once():
    s, c = _bare_session(), _Client([200])
    assert asyncio.run(s._post_result(c, "sid", {"req_id": "r1", "result": {"ok": True}})) is True
    assert len(c.posts) == 1


def test_5xx_retries_then_tells_the_sink_when_it_never_lands():
    s, c, sink = _bare_session(), _Client([503]), _Sink()
    assert asyncio.run(s._post_result(c, "sid", {"req_id": "r1", "result": {"ok": True}},
                                      sink=sink, tool="bash")) is False
    assert len(c.posts) == 4                               # 1 try + 3 retries, no note body for a 5xx
    assert len(sink.notes) == 1 and "bash" in sink.notes[0] and "503" in sink.notes[0]


def test_cap_result_leaves_a_small_result_alone():
    out = {"req_id": "r1", "result": {"ok": True, "content": "hello"}}
    assert S._cap_result(out) is out


def test_cap_result_drops_a_too_big_image_and_says_so():
    out = {"req_id": "r1", "result": {"ok": True, "native_vision": True, "image_data": "A" * 300_000,
                                      "content": "⟦ pic.png ⟧"}}
    capped = S._cap_result(out, limit=100_000)
    res = capped["result"]
    assert "image_data" not in res and res["native_vision"] is False
    assert "image not sent: 300000 bytes" in res["content"]
    assert S._json_bytes(capped) <= 100_000
    assert out["result"]["image_data"]                     # the caller's dict is not mutated


def test_cap_result_cuts_big_content_with_an_honest_note():
    out = {"req_id": "r1", "result": {"ok": True, "content": 'line "quoted"\n' * 50_000}}
    capped = S._cap_result(out, limit=100_000)
    assert S._json_bytes(capped) <= 100_000
    assert capped["req_id"] == "r1" and capped["result"]["ok"] is True
    assert capped["result"]["content"].startswith('line "quoted"')
    assert "result cut: 700000 bytes" in capped["result"]["content"]


# ── a direct _serve_stream harness ───────────────────────────────────────────

class _Exec:
    def __init__(self):
        self.calls = []

    def run(self, tool, args):
        self.calls.append(tool)
        return {"ok": True, "content": f"ran {tool}"}


def _serve(monkeypatch, frames, executor, *, mode="default", sink=None, task_id="OURS", client=None):
    import webbee.stream as ST

    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        for f in frames:
            yield f

    monkeypatch.setattr(ST, "stream_frames", _stream)
    s = _bare_session()
    s.mode = mode
    client = client or _Client([200])
    sink = sink or _Sink()
    result = asyncio.run(s._serve_stream(sink, client, "sid1", "0-0", task_id, executor))
    return result, client, sink, s


def _req(rid, tool, task_id="OURS"):
    return {"type": "tool_request", "task_id": task_id, "req_id": rid, "tool": tool, "args": {}}


_FINAL = {"type": "final", "task_id": "OURS", "text": "done"}


# ── H6: an executed request is never run twice ───────────────────────────────

def test_a_request_resent_after_a_restart_reposts_the_stored_result_not_running_again(monkeypatch):
    ex1 = _Exec()
    _, c1, _, _ = _serve(monkeypatch, [_req("req-sid1-t1-0", "bash"), _FINAL], ex1)
    assert ex1.calls == ["bash"]
    # a new process (attach after a crash): the kernel re-sends the SAME pending request
    ex2 = _Exec()
    result, c2, sink, _ = _serve(monkeypatch, [_req("req-sid1-t1-0", "bash"), _FINAL], ex2)
    assert result == "done"
    assert ex2.calls == []                                  # NOT run a second time
    assert c2.posts[0][1] == c1.posts[0][1]                 # the very same result, re-posted
    assert any("already ran" in n for n in sink.notes)


def test_the_stored_record_is_written_before_the_post(monkeypatch):
    seen_on_post = []

    class _PeekClient(_Client):
        async def post(self, url, json=None, headers=None):
            seen_on_post.append(S._ResultStore("sid1", "OURS").get(json["req_id"], "edit_file"))
            return await super().post(url, json=json, headers=headers)

    _serve(monkeypatch, [_req("r1", "edit_file"), _FINAL], _Exec(), client=_PeekClient([200]))
    assert seen_on_post and seen_on_post[0]["result"]["content"] == "ran edit_file"


def test_another_turn_with_the_same_req_id_still_runs(monkeypatch):
    ex = _Exec()
    _serve(monkeypatch, [_req("r1", "bash"), _FINAL], ex)
    _serve(monkeypatch, [_req("r1", "bash", task_id="NEXT"),
                         {"type": "final", "task_id": "NEXT", "text": "ok"}], ex, task_id="NEXT")
    assert ex.calls == ["bash", "bash"]                     # task_id is part of the key


def test_store_started_mark_answers_outcome_unknown():
    st = S._ResultStore("sid1", "OURS")
    st.mark_started("r1", "bash")
    got = st.get("r1", "bash")
    assert got["req_id"] == "r1" and got["result"]["ok"] is False
    assert "outcome is unknown" in got["result"]["content"] and "not run again" in got["result"]["content"]


def test_store_checks_tool_and_needs_a_task_id():
    st = S._ResultStore("sid1", "OURS")
    st.put("r1", "bash", {"req_id": "r1", "result": {"ok": True, "content": "x"}})
    assert st.get("r1", "bash")["result"]["content"] == "x"
    assert st.get("r1", "edit_file") is None               # a different tool is never this request
    off = S._ResultStore("sid1", "")
    off.put("r1", "bash", {"req_id": "r1", "result": {"ok": True}})
    assert off.get("r1", "bash") is None                    # legacy kernel (no task_id): memory only


def test_store_keeps_only_the_last_entries(monkeypatch):
    import os
    monkeypatch.setattr(S, "_RESULT_KEEP", 5)
    st = S._ResultStore("sid1", "OURS")
    for i in range(12):
        st.put(f"r{i}", "read_file", {"req_id": f"r{i}", "result": {"ok": True}})
        os.utime(st._path(f"r{i}"), (1000 + i, 1000 + i))   # deterministic age order
    assert len([n for n in os.listdir(st.dir) if n.endswith(".json")]) <= 5
    assert st.get("r11", "read_file") is not None and st.get("r0", "read_file") is None


def test_store_is_under_the_isolated_cache_dir(tmp_path):
    st = S._ResultStore("sid1", "OURS")
    assert st.dir.startswith(str(tmp_path))


def test_store_ignores_a_corrupt_record():
    import os
    st = S._ResultStore("sid1", "OURS")
    os.makedirs(st.dir, exist_ok=True)
    with open(st._path("r1"), "w") as f:
        f.write("{not json")
    assert st.get("r1", "bash") is None


# ── H3: a cancelled tool's command is killed ─────────────────────────────────

class _BlockingExec:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = 0

    def run(self, tool, args):
        self.started.set()
        self.release.wait(5)
        return {"ok": True, "content": "late"}

    def cancel_running(self):
        self.cancelled += 1
        self.release.set()
        return 1


def test_esc_during_a_tool_kills_its_command_and_lets_the_cancel_through(monkeypatch):
    import webbee.stream as ST
    ex = _BlockingExec()

    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield _req("r1", "bash")
        yield _FINAL

    monkeypatch.setattr(ST, "stream_frames", _stream)
    s = _bare_session()
    client = _Client([200])

    async def _go():
        task = asyncio.ensure_future(s._serve_stream(_Sink(), client, "sid1", "0-0", "OURS", ex))
        while not ex.started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_go())
    assert ex.cancelled == 1
    assert client.posts == []                               # nothing posted for the killed run


def test_stop_kills_the_running_commands_even_without_a_session():
    s = _bare_session()
    ex = _BlockingExec()
    s._executor = ex
    asyncio.run(s.stop())
    assert ex.cancelled == 1


def test_stop_with_an_executor_without_cancel_running_is_harmless():
    s = _bare_session()
    s._executor = _Exec()
    asyncio.run(s.stop())                                   # must not raise


# ── M3: plan mode refuses write-tier tools locally ───────────────────────────

def test_plan_mode_refuses_a_write_tier_tool_with_an_honest_result(monkeypatch):
    ex = _Exec()
    result, client, sink, _ = _serve(monkeypatch, [_req("r1", "bash"), _req("r2", "read_file"), _FINAL],
                                     ex, mode="plan")
    assert result == "done"
    assert ex.calls == ["read_file"]                        # the read ran, the bash did not
    refused = client.posts[0][1]
    assert refused["req_id"] == "r1" and refused["result"]["ok"] is False
    assert refused["result"]["content"] == "plan mode: bash changes files/runs commands -- not run"
    assert sink.blocked == ["bash"]


def test_plan_mode_uses_the_executor_write_tier():
    from webbee.tools import _WRITE_TIER
    assert {"write_file", "edit_file", "bash"} <= _WRITE_TIER


@pytest.mark.parametrize("mode", ["default", "autopilot"])
def test_other_modes_still_run_write_tools(monkeypatch, mode):
    ex = _Exec()
    _serve(monkeypatch, [_req("r1", "edit_file"), _FINAL], ex, mode=mode)
    assert ex.calls == ["edit_file"]


# ── M7: kernel silence on this turn ──────────────────────────────────────────

def test_kernel_silence_is_told_then_ends_the_turn(monkeypatch):
    import webbee.stream as ST
    monkeypatch.setattr(S, "_KERNEL_QUIET_NOTE_S", 0.05)
    monkeypatch.setattr(S, "_KERNEL_QUIET_END_S", 0.2)

    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        yield {"type": "progress", "task_id": "OURS", "text": "working"}
        await asyncio.Event().wait()                        # the kernel workflow died
        yield _FINAL

    monkeypatch.setattr(ST, "stream_frames", _stream)
    s, sink = _bare_session(), _Sink()
    result = asyncio.run(s._serve_stream(sink, _Client([200]), "sid1", "0-0", "OURS", _Exec()))
    assert result == ""
    assert len(sink.notes) == 2
    assert "still waiting" in sink.notes[0] and "ended here" in sink.notes[1]


def test_own_frames_keep_the_silence_clock_from_tripping(monkeypatch):
    import webbee.stream as ST
    monkeypatch.setattr(S, "_KERNEL_QUIET_NOTE_S", 0.15)
    monkeypatch.setattr(S, "_KERNEL_QUIET_END_S", 0.3)

    async def _stream(client, session_id, headers_provider, *, start_id="0-0", **_kw):
        for _ in range(6):                                  # 0.6 s in all, never 0.15 s quiet
            await asyncio.sleep(0.08)
            yield {"type": "progress", "task_id": "OURS", "text": "tick"}
        yield _FINAL

    monkeypatch.setattr(ST, "stream_frames", _stream)
    s, sink = _bare_session(), _Sink()
    assert asyncio.run(s._serve_stream(sink, _Client([200]), "sid1", "0-0", "OURS", _Exec())) == "done"
    assert sink.notes == []


# ── M7: a cleanly closed stream backs off ────────────────────────────────────

def test_clean_close_reconnects_with_doubling_backoff_reset_by_a_frame(monkeypatch):
    import webbee.stream as ST

    class _FakeSSE:
        def __init__(self, data):
            self.id, self._data = "", data

        def json(self):
            return json.loads(self._data)

    # connects: empty, empty, empty, one frame, empty, then a final
    script = [[], [], [], ['{"type": "progress"}'], [], ['{"type": "final"}']]

    class _ES:
        def __init__(self, events):
            self.events = events

        async def aiter_sse(self):
            for d in self.events:
                yield _FakeSSE(d)

    class _Ctx:
        def __init__(self, events):
            self.es = _ES(events)

        async def __aenter__(self):
            return self.es

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ST, "aconnect_sse", lambda client, m, url, headers=None, **kw: _Ctx(script.pop(0)))
    sleeps = []

    async def _sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(ST.asyncio, "sleep", _sleep)

    async def headers():
        return {}

    async def _go():
        got = []
        async for f in ST.stream_frames(object(), "s1", headers):
            got.append(f["type"])
            if f["type"] == "final":
                break
        return got

    try:
        assert asyncio.run(_go()) == ["progress", "final"]
    finally:
        monkeypatch.undo()
    # three clean closes double; the frame resets; the close after it starts over
    assert sleeps[:5] == [0.5, 1.0, 2.0, 0.5, 1.0]


def test_clean_close_backoff_is_capped():
    import webbee.stream as ST
    d = ST._CLEAN_CLOSE_BASE
    for _ in range(20):
        d = min(d * 2, ST._CLEAN_CLOSE_MAX)
    assert d == ST._CLEAN_CLOSE_MAX == 10.0


def test_plan_mode_runs_only_the_tools_that_read():
    """Security review 2026-10-30: a denylist let rollback / checkpoint (and any new tool) run in plan mode."""
    from webbee.tools import PLAN_SAFE_TOOLS, LocalToolExecutor
    for t in ("rollback", "checkpoint", "bash", "shell", "write_file", "edit_file", "multi_edit", "a_new_tool"):
        assert t not in PLAN_SAFE_TOOLS
    executor_tools = {n[3:] for n in dir(LocalToolExecutor) if n.startswith("_t_")}
    assert PLAN_SAFE_TOOLS <= executor_tools                          # every allowed name is a real tool


def test_the_result_store_is_the_users_own(tmp_path, monkeypatch):
    import os
    import stat
    import webbee.session as S
    monkeypatch.setattr(S, "_RESULT_CACHE_DIR", str(tmp_path / "results"))
    store = S._ResultStore("sess-1", "task-1")
    store.put("req-1", "read_file", {"ok": True, "content": "secret text"})
    files = [os.path.join(dp, f) for dp, _d, fs in os.walk(tmp_path / "results") for f in fs]
    assert files and all(stat.S_IMODE(os.stat(f).st_mode) == 0o600 for f in files)
    assert stat.S_IMODE(os.stat(store.dir).st_mode) == 0o700
