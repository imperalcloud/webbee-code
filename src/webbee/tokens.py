"""Serialized token acquisition for every webbee gateway call.

The gateway rotates refresh tokens SINGLE-USE (an atomic claim revokes the
presented token and mints a new pair) and the SDK's ensure_access_token has
no lock: two concurrent refreshes at an access-token expiry boundary — the
idle-steer poller vs. a starting turn, or two open terminals sharing the
same on-disk creds — race, and the loser's refresh 401s as "session
expired" (Valentin, live 2026-07-15, right after 0.3.7 made the poller a
frequent caller).

Two-layer guard:
  * an asyncio.Lock serializes ALL in-process acquisitions (poller / boot
    replay / turns never refresh concurrently);
  * on failure, ONE short-delay retry — ensure_access_token re-reads the
    creds file, so when a SIBLING terminal won the race and saved the
    rotated pair, the retry succeeds from its fresh refresh token. A real
    logged-out state still fails (both attempts), just ~0.6s later.
"""
from __future__ import annotations

import asyncio
import inspect

_RETRY_DELAY_S = 0.6

_lock = asyncio.Lock()


def make_token_provider(cfg, auth):
    """The ONE token_provider factory the REPL/marathon paths share."""

    async def token_provider() -> str:
        async with _lock:
            try:
                return await auth.ensure_access_token(cfg)
            except Exception:
                await asyncio.sleep(_RETRY_DELAY_S)
                return await auth.ensure_access_token(cfg)

    _has_force = "force" in inspect.signature(auth.ensure_access_token).parameters

    async def force_refresh() -> str:
        """ONE serialized forced refresh — the stream's 401 path (an access
        token that looks locally valid but the gateway already revoked/rotated
        it). Same lock as every acquisition; imperal-mcp < 0.5.2 (no force=)
        degrades to a normal acquisition, which is still the pre-W1 behavior."""
        async with _lock:
            if _has_force:
                return await auth.ensure_access_token(cfg, force=True)
            return await auth.ensure_access_token(cfg)

    token_provider.force_refresh = force_refresh
    return token_provider
