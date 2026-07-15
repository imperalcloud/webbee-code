"""Fetch the durable coding-thread transcript for boot replay. The gateway
keeps ONE durable per-user thread across turns/surfaces (session.py:142,
"server reloads the shared webbee-terminal thread, so context carries across
turns"); this reads its recent tail so `_boot` can replay it with origin tags
before the live loop starts. House pattern = sessions.py/remote.py: (cfg,
token_provider), lazy httpx, Bearer auth. This module does not swallow errors
itself -- `_boot` wraps the whole replay in one try/except so a network
failure never blocks/delays boot beyond the timeout, it just skips the
replay (same division of labor as remote.py + the /notify call site).
Also home to the /thread endpoint's pending-steer sibling read (liveness v2
§B) -- the drain webbee.steer polls while the REPL is idle."""
from __future__ import annotations

_DISPLAY_LIMIT = 400

# The durable thread stores each tool exchange FLATTENED into the message
# text ("[tool_use bash] {...}" / "[tool_result] ..."), so the agent can
# reread its own past work. That is mind-food, not conversation -- replaying
# it verbatim floods the boot screen with raw JSON (Valentin, live
# 2026-07-15). Replay shows only the conversational part of each message.
_FLATTEN_MARKERS = ("[tool_use ", "[tool_result]")


def conversational_text(content) -> str:
    """The human-conversation part of one stored thread message: everything
    up to the first flattened tool block, stripped. "" means the message was
    pure tool traffic and must be skipped by the replay."""
    text = str(content or "")
    cut = len(text)
    for m in _FLATTEN_MARKERS:
        i = text.find(m)
        if i != -1:
            cut = min(cut, i)
    return text[:cut].strip()


async def fetch_recent_thread(cfg, token_provider, session_id: str) -> list[dict]:
    import httpx
    token = await token_provider()
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.get(f"/v1/agent/sessions/{session_id}/thread",
                        headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return (r.json() or {}).get("messages", [])


async def fetch_pending_steer(cfg, token_provider, session_id: str) -> list[dict]:
    """Drain this user's queued remote instructions (idle-steer pickup,
    liveness v2 §B) -- the /thread endpoint's sibling, same auth. The gateway
    read is DESTRUCTIVE: each queued item is returned exactly ONCE, oldest
    first (empty when nothing is queued or remote control is disabled), so
    the caller owns every item it receives. Non-swallowing like
    fetch_recent_thread above: the poller (webbee.steer) wraps each tick in
    its own try/except."""
    import httpx
    token = await token_provider()
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.get(f"/v1/agent/sessions/{session_id}/pending-steer",
                        headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return (r.json() or {}).get("items", [])


def truncate_for_display(text, limit: int = _DISPLAY_LIMIT) -> str:
    """Cap one replayed message's text so a single huge assistant reply can't
    flood the boot screen -- foreign_turn renders one line per message."""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
