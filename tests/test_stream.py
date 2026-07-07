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
