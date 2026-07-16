import asyncio

import httpx

from webbee.thread import fetch_recent_thread, truncate_for_display


class _Cfg:
    api_url = "http://x"


async def _tp():
    return "tok"


def test_fetch_recent_thread_gets_right_path_and_bearer(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"messages": [{"role": "user", "content": "hi", "surface": "telegram"}]}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            seen["path"] = path
            seen["headers"] = headers
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    out = asyncio.run(fetch_recent_thread(_Cfg(), _tp, "marathon-user-1-rboot"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rboot/thread"
    assert seen["headers"] == {"Authorization": "Bearer tok"}
    assert out == [{"role": "user", "content": "hi", "surface": "telegram"}]


def test_fetch_recent_thread_empty_response_returns_empty_list(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    assert asyncio.run(fetch_recent_thread(_Cfg(), _tp, "marathon-user-1-rboot")) == []


def test_fetch_recent_thread_propagates_network_error():
    # No transport mocked -- unreachable host raises. This module never
    # swallows errors itself; the caller (`_boot`) owns the best-effort
    # try/except (same division of labor as remote.py + get_step_detail's
    # non-swallowing siblings in this package).
    class _UnreachableCfg:
        api_url = "http://127.0.0.1:1"

    try:
        asyncio.run(fetch_recent_thread(_UnreachableCfg(), _tp, "marathon-user-1-rboot"))
        assert False, "expected a network error to propagate"
    except Exception:
        pass


def test_fetch_recent_thread_raises_on_http_error(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    try:
        asyncio.run(fetch_recent_thread(_Cfg(), _tp, "marathon-user-1-rboot"))
        assert False, "expected raise_for_status's error to propagate"
    except httpx.HTTPStatusError:
        pass


# ── pending-steer fetcher (liveness v2 §B) ────────────────────────────────────

def test_fetch_pending_steer_gets_right_path_and_bearer(monkeypatch):
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"text": "push the fix", "surface": "telegram", "ts": 1}]}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            seen["path"] = path
            seen["headers"] = headers
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    out = asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer"
    assert seen["headers"] == {"Authorization": "Bearer tok"}
    # Full payload verbatim: items + (absent here) requested_mode ride together.
    assert out == {"items": [{"text": "push the fix", "surface": "telegram", "ts": 1}]}


def test_fetch_pending_steer_empty_response_returns_empty_payload(monkeypatch):
    from webbee.thread import fetch_pending_steer

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    # A null body degrades to {} — the poller's .get("items")/.get(
    # "requested_mode") reads then see nothing, never a crash.
    assert asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56")) == {}


def test_fetch_pending_steer_returns_requested_mode_verbatim(monkeypatch):
    # Full-queue-layer mode adoption: the gateway's one-shot requested_mode
    # (GETDEL server-side — delivered exactly once) rides the SAME payload;
    # the client must relay it verbatim for webbee.steer to consume.
    from webbee.thread import fetch_pending_steer

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [],
                    "requested_mode": {"mode": "autopilot", "surface": "telegram"}}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    out = asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56"))
    assert out["requested_mode"] == {"mode": "autopilot", "surface": "telegram"}


def test_fetch_pending_steer_drain_is_gateway_side_no_client_cache(monkeypatch):
    # The gateway read is DESTRUCTIVE (each item returned exactly once): the
    # client must relay each response verbatim and never re-serve a previously
    # drained item from any cache of its own.
    from webbee.thread import fetch_pending_steer
    payloads = [[{"text": "one", "surface": "telegram", "ts": 1}], []]

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            items = payloads.pop(0)

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"items": items}

            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    first = asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56"))
    second = asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56"))
    assert first == {"items": [{"text": "one", "surface": "telegram", "ts": 1}]}
    assert second == {"items": []}


def test_fetch_pending_steer_propagates_network_error():
    # Non-swallowing, same division of labor as fetch_recent_thread: the
    # poller (webbee.steer) owns the per-tick try/except.
    from webbee.thread import fetch_pending_steer

    class _UnreachableCfg:
        api_url = "http://127.0.0.1:1"

    try:
        asyncio.run(fetch_pending_steer(_UnreachableCfg(), _tp, "marathon-user-1-rab12cd34ef56"))
        assert False, "expected a network error to propagate"
    except Exception:
        pass


def test_truncate_for_display_short_text_unchanged():
    assert truncate_for_display("hello") == "hello"


def test_truncate_for_display_none_becomes_empty_string():
    assert truncate_for_display(None) == ""


def test_truncate_for_display_long_text_capped_with_ellipsis():
    text = "a" * 1000
    out = truncate_for_display(text)
    assert len(out) == 401           # 400 chars + ellipsis
    assert out.endswith("…")
    assert out[:400] == "a" * 400


def test_truncate_for_display_respects_custom_limit():
    assert truncate_for_display("abcdef", limit=3) == "abc…"


def test_conversational_text_plain_message_untouched():
    from webbee.thread import conversational_text
    assert conversational_text("почини тесты") == "почини тесты"


def test_conversational_text_pure_tool_traffic_skipped():
    from webbee.thread import conversational_text
    assert conversational_text('[tool_result] {"content": "COMPILE_OK", "ok": true}') == ""
    assert conversational_text('[tool_use bash] {"command": "pytest -q"}') == ""


def test_conversational_text_narration_kept_tool_tail_cut():
    from webbee.thread import conversational_text
    mixed = 'Всё компилируется чисто. Теперь коммичу.\n[tool_use bash] {"command": "git push"}'
    assert conversational_text(mixed) == "Всё компилируется чисто. Теперь коммичу."


def test_conversational_text_none_is_empty():
    from webbee.thread import conversational_text
    assert conversational_text(None) == ""
