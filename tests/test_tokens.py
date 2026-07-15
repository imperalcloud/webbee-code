"""Serialized token provider (webbee.tokens) — the anti-lockout auth guard."""
import asyncio

import pytest

from webbee.tokens import make_token_provider


class _Auth:
    def __init__(self, fail_first: int = 0):
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.fail_first = fail_first

    async def ensure_access_token(self, cfg):
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0.01)
            if self.calls <= self.fail_first:
                raise RuntimeError("invalid refresh token")
            return f"tok{self.calls}"
        finally:
            self.concurrent -= 1


def test_concurrent_acquisitions_serialized():
    auth = _Auth()
    tp = make_token_provider(object(), auth)

    async def go():
        return await asyncio.gather(tp(), tp(), tp())

    toks = asyncio.run(go())
    assert all(t.startswith("tok") for t in toks)
    assert auth.max_concurrent == 1          # the lock: never two refreshes at once


def test_failure_retries_once_after_sibling_rotation(monkeypatch):
    import webbee.tokens as wt
    monkeypatch.setattr(wt, "_RETRY_DELAY_S", 0)
    auth = _Auth(fail_first=1)               # first attempt loses the rotation race
    tp = make_token_provider(object(), auth)
    assert asyncio.run(tp()) == "tok2"       # retry re-reads (sibling saved) and wins
    assert auth.calls == 2


def test_true_logged_out_still_fails(monkeypatch):
    import webbee.tokens as wt
    monkeypatch.setattr(wt, "_RETRY_DELAY_S", 0)
    auth = _Auth(fail_first=99)
    tp = make_token_provider(object(), auth)
    with pytest.raises(RuntimeError):
        asyncio.run(tp())
    assert auth.calls == 2                   # exactly one retry, no hammering
