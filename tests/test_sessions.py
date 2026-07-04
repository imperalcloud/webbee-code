import asyncio

import webbee.sessions as S


def test_list_sessions_parses(monkeypatch):
    async def fake_get(cfg, tp, path):
        assert path == "/v1/auth/sessions"
        return {"sessions": [{"session_id": "a", "surface": "cli", "current": True}]}
    monkeypatch.setattr(S, "_get", fake_get)
    out = asyncio.run(S.list_sessions(None, None))
    assert out[0]["session_id"] == "a"


def test_list_sessions_network_error_returns_empty(monkeypatch):
    async def boom(*a):
        raise RuntimeError("net")
    monkeypatch.setattr(S, "_get", boom)
    assert asyncio.run(S.list_sessions(None, None)) == []


def test_revoke_session_posts_right_path(monkeypatch):
    seen = {}
    async def fake_post(cfg, tp, path):
        seen["path"] = path
        return {"status": "revoked"}
    monkeypatch.setattr(S, "_post", fake_post)
    assert asyncio.run(S.revoke_session(None, None, "sid1")) is True
    assert seen["path"] == "/v1/auth/sessions/sid1/revoke"


def test_revoke_session_error_returns_false(monkeypatch):
    async def boom(*a):
        raise RuntimeError()
    monkeypatch.setattr(S, "_post", boom)
    assert asyncio.run(S.revoke_session(None, None, "x")) is False


def test_revoke_others_count(monkeypatch):
    async def fake_post(cfg, tp, path):
        assert path == "/v1/auth/sessions/revoke-others"
        return {"revoked": 3}
    monkeypatch.setattr(S, "_post", fake_post)
    assert asyncio.run(S.revoke_others(None, None)) == 3


def test_revoke_others_error_returns_minus_one(monkeypatch):
    async def boom(*a):
        raise RuntimeError()
    monkeypatch.setattr(S, "_post", boom)
    assert asyncio.run(S.revoke_others(None, None)) == -1
