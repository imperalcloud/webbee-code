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


# ── applied-mode report (T6.2) ────────────────────────────────────────────────

def test_fetch_pending_steer_appends_mode_query_param_when_given(monkeypatch):
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            seen["path"] = path
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56", mode="plan"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer?mode=plan"


def test_fetch_pending_steer_omits_mode_query_param_when_absent(monkeypatch):
    # Default "" -- an older gateway sees the EXACT same request as before
    # this feature (no trailing "?mode=" at all).
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            seen["path"] = path
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer"


def test_fetch_pending_steer_mode_param_works_with_shared_client(monkeypatch):
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeClient:
        async def request(self, method, path, json=None, headers=None):
            seen["path"] = path
            return _Resp()

    asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56",
                                    client=FakeClient(), mode="autopilot"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer?mode=autopilot"


# ── W4c T3: label sync -- &label= beside ?mode=, same back-compat style ─────

def test_fetch_pending_steer_appends_label_query_param_when_given(monkeypatch):
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            seen["path"] = path
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56", label="billing fix"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer?label=billing%20fix"


def test_fetch_pending_steer_omits_label_query_param_when_absent(monkeypatch):
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            seen["path"] = path
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer"


def test_fetch_pending_steer_combines_mode_and_label_with_ampersand(monkeypatch):
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path, headers=None, **kw):
            seen["path"] = path
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56",
                                    mode="plan", label="billing"))
    assert seen["path"] == ("/v1/agent/sessions/marathon-user-1-rab12cd34ef56/"
                            "pending-steer?mode=plan&label=billing")


def test_fetch_pending_steer_label_param_works_with_shared_client(monkeypatch):
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeClient:
        async def request(self, method, path, json=None, headers=None):
            seen["path"] = path
            return _Resp()

    asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56",
                                    client=FakeClient(), label="billing"))
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer?label=billing"


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


# ── shared keep-alive client (Task 12: no more TCP+TLS handshake every 4s) ──

def test_fetch_pending_steer_reuses_given_client(monkeypatch):
    # With a client the repl already owns, fetch_pending_steer must call
    # THROUGH it (client.request) and never open a fresh httpx.AsyncClient --
    # the constructor is monkeypatched to blow up if it's touched.
    from webbee.thread import fetch_pending_steer
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": []}

    class FakeClient:
        async def request(self, method, path, json=None, headers=None):
            seen["method"] = method
            seen["path"] = path
            seen["headers"] = headers
            return _Resp()

    def _no_new_client(*a, **kw):
        raise AssertionError("must not construct a new AsyncClient when one is given")

    monkeypatch.setattr(httpx, "AsyncClient", _no_new_client)
    out = asyncio.run(fetch_pending_steer(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56",
                                          client=FakeClient()))
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/pending-steer"
    assert seen["headers"] == {"Authorization": "Bearer tok"}
    assert out == {"items": []}


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


# ── 0.3.15: mid-turn inject POST (the /thread family's write sibling) ─────────
# inject_to_session is the gateway leg the dock fires on Enter-while-busy:
# POST /v1/agent/sessions/{id}/inject {text, steer_iid} → task_id-LESS
# new_task into the user's own RUNNING session. Non-swallowing like its
# siblings — repl._inject_via_gateway wraps it (local-queue fallback).

def test_inject_to_session_posts_right_path_bearer_and_body(monkeypatch):
    from webbee.thread import inject_to_session
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True, "steer_iid": "iid-1"}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): seen["base_url"] = kw.get("base_url")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, path, json=None, headers=None):
            seen["path"] = path; seen["json"] = json; seen["headers"] = headers
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    ok = asyncio.run(inject_to_session(_Cfg(), _tp, "marathon-user-1-rab12cd34ef56",
                                       "fly this in", "iid-1"))
    assert ok is True
    assert seen["base_url"] == "http://x"
    assert seen["path"] == "/v1/agent/sessions/marathon-user-1-rab12cd34ef56/inject"
    assert seen["json"] == {"text": "fly this in", "steer_iid": "iid-1"}
    assert seen["headers"] == {"Authorization": "Bearer tok"}


def test_inject_to_session_gateway_refusal_returns_false(monkeypatch):
    from webbee.thread import inject_to_session

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": False}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, path, json=None, headers=None): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    assert asyncio.run(inject_to_session(_Cfg(), _tp, "s1", "x", "i")) is False


def test_inject_to_session_raises_on_http_error_non_swallowing(monkeypatch):
    from webbee.thread import inject_to_session

    class _Resp:
        def raise_for_status(self): raise httpx.HTTPStatusError("401", request=None, response=None)
        def json(self): return {}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, path, json=None, headers=None): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    try:
        asyncio.run(inject_to_session(_Cfg(), _tp, "s1", "x", "i"))
        raised = False
    except httpx.HTTPStatusError:
        raised = True
    assert raised    # the caller (repl wiring) owns the fallback


def test_inject_to_session_reuses_given_client_forwards_body(monkeypatch):
    # With a client the repl already owns (Task 12's shared keep-alive
    # client), inject_to_session must call THROUGH it (client.request) and
    # forward the {text, steer_iid} body verbatim -- and never open a fresh
    # httpx.AsyncClient (the constructor is monkeypatched to blow up if
    # touched, same discipline as fetch_pending_steer's sibling test above).
    from webbee.thread import inject_to_session
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    class FakeClient:
        async def request(self, method, path, json=None, headers=None):
            seen["method"] = method
            seen["path"] = path
            seen["json"] = json
            seen["headers"] = headers
            return _Resp()

    def _no_new_client(*a, **kw):
        raise AssertionError("must not construct a new AsyncClient when one is given")

    monkeypatch.setattr(httpx, "AsyncClient", _no_new_client)
    ok = asyncio.run(inject_to_session(_Cfg(), _tp, "s1", "fly this in", "iid-1",
                                       client=FakeClient()))
    assert ok is True
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/agent/sessions/s1/inject"
    assert seen["json"] == {"text": "fly this in", "steer_iid": "iid-1"}
    assert seen["headers"] == {"Authorization": "Bearer tok"}
