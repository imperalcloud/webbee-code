"""Fetch the durable coding-thread transcript for boot replay. The gateway
keeps ONE durable per-user thread across turns/surfaces (session.py:142,
"server reloads the shared webbee-terminal thread, so context carries across
turns"); this reads its recent tail so `_boot` can replay it with origin tags
before the live loop starts. House pattern = sessions.py/remote.py: (cfg,
token_provider), lazy httpx, Bearer auth. This module does not swallow errors
itself -- `_boot` wraps the whole replay in one try/except so a network
failure never blocks/delays boot beyond the timeout, it just skips the
replay (same division of labor as remote.py + the /notify call site)."""
from __future__ import annotations

_DISPLAY_LIMIT = 400


async def fetch_recent_thread(cfg, token_provider, session_id: str) -> list[dict]:
    import httpx
    token = await token_provider()
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.get(f"/v1/agent/sessions/{session_id}/thread",
                        headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return (r.json() or {}).get("messages", [])


def truncate_for_display(text, limit: int = _DISPLAY_LIMIT) -> str:
    """Cap one replayed message's text so a single huge assistant reply can't
    flood the boot screen -- foreign_turn renders one line per message."""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
