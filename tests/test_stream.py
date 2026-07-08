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
