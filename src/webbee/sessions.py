"""Sessions client — list/revoke the user's Imperal Cloud sessions from the
terminal. Talks to the auth gateway with the user's access token (Bearer), so
the gateway marks THIS terminal as the current session via its `sid` claim.
Best-effort: network errors never raise — they return empty/False so the REPL
can note the failure instead of crashing."""
from __future__ import annotations


async def _get(cfg, token_provider, path: str) -> dict:
    import httpx
    token = await token_provider()
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


async def _post(cfg, token_provider, path: str) -> dict:
    import httpx
    token = await token_provider()
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.post(path, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json() if r.content else {}


async def list_sessions(cfg, token_provider) -> list[dict]:
    try:
        return (await _get(cfg, token_provider, "/v1/auth/sessions")).get("sessions", [])
    except Exception:
        return []


async def revoke_session(cfg, token_provider, session_id: str) -> bool:
    try:
        await _post(cfg, token_provider, f"/v1/auth/sessions/{session_id}/revoke")
        return True
    except Exception:
        return False


async def revoke_others(cfg, token_provider) -> int:
    """Revoke every session except this terminal's. Returns the count, or -1 on
    error (the gateway keeps the current session, identified by the JWT sid)."""
    try:
        return int((await _post(cfg, token_provider, "/v1/auth/sessions/revoke-others")).get("revoked", 0))
    except Exception:
        return -1
