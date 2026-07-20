"""Boot reattach notice (T6.3): fetch_active_sessions is the best-effort
gateway client (house pattern -- (cfg, token_provider), Bearer auth, []
on ANY failure); boot_reattach_notice is the pure decision logic that turns
its listing into 0-2 sink.note lines. repl wiring (_note_reattach) is
covered in test_repl.py."""
import asyncio

import httpx

from webbee.active_sessions import boot_reattach_notice, fetch_active_sessions


class _Cfg:
    api_url = "http://x"


async def _tp():
    return "tok"


# ── fetch_active_sessions ─────────────────────────────────────────────────────

def test_fetch_active_sessions_gets_right_path_and_bearer(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"sessions": [{"session_id": "marathon-u-rabc"}]}

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
    out = asyncio.run(fetch_active_sessions(_Cfg(), _tp))
    assert seen["path"] == "/v1/agent/sessions/active"
    assert seen["headers"] == {"Authorization": "Bearer tok"}
    assert out == [{"session_id": "marathon-u-rabc"}]


def test_fetch_active_sessions_empty_response_returns_empty_list(monkeypatch):
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
    assert asyncio.run(fetch_active_sessions(_Cfg(), _tp)) == []


def test_fetch_active_sessions_network_error_returns_empty_list_best_effort():
    # Unlike thread.py's fetchers, this one IS best-effort by contract (no
    # boot._boot-equivalent wraps it) -- an unreachable host must degrade to
    # [], never raise (an older gateway without the route, or a network
    # blip, must never delay/crash boot).
    class _UnreachableCfg:
        api_url = "http://127.0.0.1:1"

    assert asyncio.run(fetch_active_sessions(_UnreachableCfg(), _tp)) == []


def test_fetch_active_sessions_http_error_returns_empty_list(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("boom", request=None, response=None)

        def json(self):
            return {}

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
    assert asyncio.run(fetch_active_sessions(_Cfg(), _tp)) == []


def test_fetch_active_sessions_token_provider_failure_returns_empty_list():
    async def boom():
        raise RuntimeError("not logged in")

    assert asyncio.run(fetch_active_sessions(_Cfg(), boom)) == []


def test_fetch_active_sessions_reuses_given_client():
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"sessions": []}

    class FakeClient:
        async def get(self, path, headers=None):
            seen["path"] = path
            seen["headers"] = headers
            return _Resp()

    out = asyncio.run(fetch_active_sessions(_Cfg(), _tp, client=FakeClient()))
    assert seen["path"] == "/v1/agent/sessions/active"
    assert out == []


# ── boot_reattach_notice (pure decision logic) ────────────────────────────────

def test_no_sessions_no_notice():
    assert boot_reattach_notice([], "abc123") == []


def test_own_repo_running_session_notes_reattach():
    sessions = [{"session_id": "marathon-user-1-rabc123"}]
    lines = boot_reattach_notice(sessions, "abc123")
    assert len(lines) == 1
    assert "reattached" in lines[0] and "history" in lines[0]


def test_own_repo_running_and_pending_consent_adds_second_line():
    sessions = [{"session_id": "marathon-user-1-rabc123", "pending_consent": {"tool": "bash"}}]
    lines = boot_reattach_notice(sessions, "abc123")
    assert len(lines) == 2
    assert "reattached" in lines[0]
    assert "approval" in lines[1] and "panel" in lines[1]


def test_other_repo_parked_session_notes_one_pointer_no_internals():
    sessions = [{"session_id": "marathon-user-1-rzzz999", "pending_consent": {"tool": "bash"}}]
    lines = boot_reattach_notice(sessions, "abc123")
    assert len(lines) == 1
    assert "parked session waiting for approval in another repo" in lines[0]
    # No internals leaked: no raw session id, no tool name from pending_consent.
    assert "rzzz999" not in lines[0] and "bash" not in lines[0]


def test_other_repo_running_without_pending_consent_is_silent():
    sessions = [{"session_id": "marathon-user-1-rzzz999"}]
    assert boot_reattach_notice(sessions, "abc123") == []


def test_multiple_other_repo_parked_sessions_collapse_to_one_line():
    sessions = [
        {"session_id": "marathon-user-1-rzzz999", "pending_consent": {"tool": "bash"}},
        {"session_id": "coding-user-1-ryyy888", "pending_consent": {"tool": "edit"}},
    ]
    lines = boot_reattach_notice(sessions, "abc123")
    assert len(lines) == 1


def test_own_repo_and_other_repo_parked_both_present():
    sessions = [
        {"session_id": "marathon-user-1-rabc123"},
        {"session_id": "coding-user-1-ryyy888", "pending_consent": {"tool": "edit"}},
    ]
    lines = boot_reattach_notice(sessions, "abc123")
    assert len(lines) == 2
    assert "reattached" in lines[0]
    assert "another repo" in lines[1]


def test_session_id_missing_or_malformed_never_crashes():
    sessions = [{}, {"session_id": None}, {"session_id": 12345}]
    assert boot_reattach_notice(sessions, "abc123") == []
