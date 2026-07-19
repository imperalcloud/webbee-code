"""U0 reconnecting listener: yields frames, records last id, reconnects with
Last-Event-ID after a mid-stream drop, and stops on generator close."""
import asyncio

import httpx
import pytest

from webbee.stream import stream_frames


class _FakeSSE:
    def __init__(self, sse_id, data):
        self.id = sse_id
        self._data = data

    def json(self):
        import json
        return json.loads(self._data)


class _Script:
    """Each connect() pops one script entry: list of (id, json) then an
    optional exception to raise mid-stream."""
    def __init__(self, connects):
        self.connects = list(connects)
        self.seen_headers: list = []


def _fake_aconnect(script):
    class _ES:
        def __init__(self, events, err):
            self._events, self._err = events, err

        async def aiter_sse(self):
            for sse_id, data in self._events:
                yield _FakeSSE(sse_id, data)
            if self._err:
                raise self._err

    class _Ctx:
        def __init__(self, events, err):
            self._es = _ES(events, err)

        async def __aenter__(self):
            return self._es

        async def __aexit__(self, *a):
            return False

    def _connect(client, method, url, headers=None, **kw):
        script.seen_headers.append(dict(headers or {}))
        events, err = script.connects.pop(0)
        return _Ctx(events, err)

    return _connect


async def test_reconnects_with_last_event_id(monkeypatch):
    import webbee.stream as S
    script = _Script([
        ([("1-0", '{"type": "progress", "text": "a"}')], httpx.ReadError("drop")),
        ([("2-0", '{"type": "final", "text": "b"}')], None),
    ])
    monkeypatch.setattr(S, "aconnect_sse", _fake_aconnect(script))
    monkeypatch.setattr(S, "_BACKOFF_BASE", 0.001)  # fast test

    async def headers():
        return {"Authorization": "Bearer t"}

    got = []
    async for frame in stream_frames(object(), "s1", headers, start_id="0-0"):
        got.append(frame)
        if frame.get("type") == "final":
            break
    assert [f["type"] for f in got] == ["progress", "final"]
    # first connect: start_id; second connect: resumed from the LAST SEEN id
    assert script.seen_headers[0]["Last-Event-ID"] == "0-0"
    assert script.seen_headers[1]["Last-Event-ID"] == "1-0"


async def test_malformed_frame_is_skipped_not_fatal(monkeypatch):
    """C2: one non-JSON SSE frame must not kill the turn -- it's skipped and
    the next valid frame still comes through."""
    import webbee.stream as S
    script = _Script([
        ([("1-0", "not json"), ("2-0", '{"type": "final", "text": "ok"}')], None),
    ])
    monkeypatch.setattr(S, "aconnect_sse", _fake_aconnect(script))
    monkeypatch.setattr(S, "_BACKOFF_BASE", 0.001)

    async def headers():
        return {"Authorization": "Bearer t"}

    got = []
    async for frame in stream_frames(object(), "s1", headers, start_id="0-0"):
        got.append(frame)
        if frame.get("type") == "final":
            break
    assert [f["type"] for f in got] == ["final"]  # malformed frame skipped, not yielded


class _FakeAuthResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    async def aread(self):
        return b""


class _ESWithResponse:
    def __init__(self, status_code):
        self.response = _FakeAuthResponse(status_code)

    async def aiter_sse(self):
        return
        yield  # pragma: no cover - unreachable; raise happens before this is called


class _CtxWithResponse:
    def __init__(self, status_code):
        self._es = _ESWithResponse(status_code)

    async def __aenter__(self):
        return self._es

    async def __aexit__(self, *a):
        return False


async def test_403_raises_stream_auth_error_and_does_not_retry(monkeypatch):
    """C1: a non-retryable 4xx (expired token / ownership 403) must surface
    immediately as StreamAuthError, NOT be swallowed and retried forever."""
    import webbee.stream as S
    from webbee.stream import StreamAuthError

    calls = []

    def _connect(client, method, url, headers=None, **kw):
        calls.append(1)
        return _CtxWithResponse(403)

    monkeypatch.setattr(S, "aconnect_sse", _connect)
    monkeypatch.setattr(S, "_BACKOFF_BASE", 0.001)

    async def headers():
        return {"Authorization": "Bearer t"}

    with pytest.raises(StreamAuthError):
        async for _ in stream_frames(object(), "s1", headers, start_id="0-0"):
            pass

    assert calls == [1]  # connected exactly once -- did NOT loop/retry


# --- verdict/transient split (401/403/404 raise; everything else retries) ---


class _FakeSSEDict:
    """One SSE frame whose payload is already a dict (test convenience --
    the real gateway sends JSON text; _parse_frame only needs .json())."""
    def __init__(self, sse_id, data):
        self.id = sse_id
        self._data = data

    def json(self):
        return self._data


class _FakeStatusSource:
    """A connect that resolves to a bare status code -- a 401/403/404
    verdict, or a transient 5xx/408/429 -- with no frames."""
    def __init__(self, status_code):
        self.response = _FakeAuthResponse(status_code)

    async def aiter_sse(self):
        return
        yield  # pragma: no cover - unreachable; raise happens before this


class _FakeFrameSource:
    """A connect that streams the given frames (dicts with 'id'/'data') then
    closes cleanly -- no .response attribute, so stream.py treats it as 2xx."""
    def __init__(self, frames):
        self._frames = frames

    async def aiter_sse(self):
        for f in self._frames:
            yield _FakeSSEDict(f.get("id"), f["data"])


class _FakeConnectCtx:
    def __init__(self, event_source):
        self._es = event_source

    async def __aenter__(self):
        return self._es

    async def __aexit__(self, *a):
        return False


class _FakeServer:
    """Pops one scripted connect per aconnect_sse call: an int entry is a
    bare status code, a list entry is a run of frames to stream successfully."""
    def __init__(self, entries):
        self._entries = list(entries)
        self.connects = 0
        self.seen_headers: list = []

    def connect(self, client, method, url, headers=None, **kw):
        self.seen_headers.append(dict(headers or {}))
        self.connects += 1
        entry = self._entries.pop(0)
        es = _FakeStatusSource(entry) if isinstance(entry, int) else _FakeFrameSource(entry)
        return _FakeConnectCtx(es)


@pytest.fixture
def fake_sse_server(monkeypatch):
    """Factory fixture: fake_sse_server([502, [...]]) patches
    webbee.stream.aconnect_sse and returns the _FakeServer for assertions."""
    def _make(entries):
        import webbee.stream as S
        server = _FakeServer(entries)
        monkeypatch.setattr(S, "aconnect_sse", server.connect)
        monkeypatch.setattr(S, "_BACKOFF_BASE", 0.001)
        monkeypatch.setattr(S, "_BACKOFF_MAX", 0.005)
        return server

    return _make


async def _default_headers_provider():
    return {"Authorization": "Bearer t"}


def collect_frames(server, *, headers_provider=None, force_refresh=None,
                    on_retry=None, stop_on="final"):
    """Drain stream_frames against a fake_sse_server-built server until the
    terminal frame, returning the list of parsed frames. Owns its own event
    loop so callers stay plain `def` tests."""
    async def _run():
        hp = headers_provider or _default_headers_provider
        got = []
        async for frame in stream_frames(
            object(), "s1", hp, start_id="0-0",
            force_refresh=force_refresh, on_retry=on_retry,
        ):
            got.append(frame)
            if frame.get("type") == stop_on:
                break
        return got

    return asyncio.run(_run())


def flaky_headers_provider(fail_first_with):
    """A headers_provider that raises the given exception on its first call
    (simulating a refresh that hit a mid-deploy gateway) then succeeds."""
    state = {"calls": 0}

    async def _provider():
        state["calls"] += 1
        if state["calls"] == 1:
            raise fail_first_with
        return {"Authorization": "Bearer t"}

    return _provider


def test_502_retries_then_resumes(fake_sse_server):
    """A transient 502 must NOT raise StreamAuthError: connect #1 -> 502,
    connect #2 -> one frame. Also asserts on_retry fired with attempt=1 then
    the online signal (0, 0.0)."""
    calls = []
    server = fake_sse_server([502, [{"id": "1-1", "data": {"type": "final", "text": "ok"}}]])
    frames = collect_frames(server, on_retry=lambda a, d: calls.append(a))
    assert [f["type"] for f in frames] == ["final"]
    assert calls[0] == 1 and calls[-1] == 0
    assert server.connects == 2


def test_401_forces_one_refresh_then_verdict(fake_sse_server):
    """401 -> force_refresh() -> ONE reconnect. Second 401 -> StreamAuthError."""
    from webbee.stream import StreamAuthError

    forced = []

    async def force():
        forced.append(True)

    server = fake_sse_server([401, 401])
    with pytest.raises(StreamAuthError):
        collect_frames(server, force_refresh=force)
    assert forced == [True]
    assert server.connects == 2


def test_transient_auth_error_from_headers_provider_retries(fake_sse_server):
    """A TransientAuthError raised by headers_provider (refresh hit a gateway
    deploy) is retried, not fatal."""
    from imperal_mcp.auth import TransientAuthError

    provider = flaky_headers_provider(fail_first_with=TransientAuthError("502"))
    server = fake_sse_server([[{"id": "1-1", "data": {"type": "final", "text": "ok"}}]])
    frames = collect_frames(server, headers_provider=provider)
    assert frames and server.connects >= 1


# ── FIX2: the refresh budget is burned only by a COMPLETED refresh ──────────
# Before the fix, `auth_retried = True` was set BEFORE `await force_refresh()`
# -- if force_refresh itself failed transiently (a gateway mid-deploy), the
# ONE-per-outage-window budget was already spent, so the retried 401 (still
# the SAME outage) hit the "second 401 is the real verdict" branch instead of
# getting its own refresh attempt.

def test_401_refresh_transient_failure_does_not_burn_budget(fake_sse_server):
    """force_refresh raises TransientAuthError on its FIRST call (gateway
    redeploying mid-rotation), succeeds on the second. Server: 401, 401, then
    frames. The turn must RESUME (frames delivered), force_refresh called
    TWICE, and no StreamAuthError -- the failed refresh attempt must not have
    burned the one-per-window budget."""
    from imperal_mcp.auth import TransientAuthError

    calls = {"n": 0}

    async def force():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientAuthError("gateway redeploying")

    server = fake_sse_server([401, 401, [{"id": "1-1", "data": {"type": "final", "text": "ok"}}]])
    frames = collect_frames(server, force_refresh=force)
    assert [f["type"] for f in frames] == ["final"]
    assert calls["n"] == 2
    assert server.connects == 3


# ── FIX3: strict 4xx taxonomy -- every 4xx except 408/429 is a verdict ──────
# Deterministic client errors (400/402/405/413/422/...) must surface
# immediately instead of being treated as transient and retried forever.

def test_400_raises_stream_auth_error_immediately(fake_sse_server):
    from webbee.stream import StreamAuthError

    server = fake_sse_server([400])
    with pytest.raises(StreamAuthError):
        collect_frames(server)
    assert server.connects == 1


# ── FIX7d coverage: the 401-refresh SUCCESS half, and re-arming across a
# later, independent outage window on the SAME stream. ──────────────────────

def test_401_refresh_success_then_frames_resume(fake_sse_server):
    """The success half of the 401-refresh dance (only the failure half --
    401,401 -> StreamAuthError -- had coverage before): 401 -> ONE successful
    force_refresh -> reconnect -> frames actually flow."""
    forced = []

    async def force():
        forced.append(True)

    server = fake_sse_server([401, [{"id": "1-1", "data": {"type": "final", "text": "ok"}}]])
    frames = collect_frames(server, force_refresh=force)
    assert [f["type"] for f in frames] == ["final"]
    assert forced == [True]
    assert server.connects == 2


def test_401_budget_rearms_for_a_later_independent_outage(fake_sse_server):
    """After a successful refresh+resume, a LATER 401 on the same stream must
    get its OWN fresh refresh (not an immediate verdict) -- the one-per-
    outage-window budget re-arms once the connection is actually back."""
    calls = []

    async def force():
        calls.append(1)

    server = fake_sse_server([
        401,
        [{"id": "1-1", "data": {"type": "progress", "text": "p"}}],
        401,
        [{"id": "2-1", "data": {"type": "final", "text": "ok"}}],
    ])
    frames = collect_frames(server, force_refresh=force)
    assert [f["type"] for f in frames] == ["progress", "final"]
    assert calls == [1, 1]          # TWO successful refreshes, not one-then-verdict
    assert server.connects == 4
