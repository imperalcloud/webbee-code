"""Remote-control client — read/set this coding session's mirror+steer routing
(Telegram / panel) via the gateway. Best-effort: network errors never raise."""
from __future__ import annotations

_ARG_TO_STATE = {
    "tg": {"enabled": True, "mirror": ["telegram"], "steer": ["telegram"]},
    "panel": {"enabled": True, "mirror": ["panel"], "steer": []},
    "both": {"enabled": True, "mirror": ["telegram", "panel"], "steer": ["telegram"]},
    "off": {"enabled": False, "mirror": [], "steer": []},
}


async def get_remote(cfg, token_provider, session_id: str) -> dict:
    import httpx
    token = await token_provider()
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.get(f"/v1/agent/sessions/{session_id}/remote",
                        headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return (r.json() or {}).get("state", {})


async def set_remote(cfg, token_provider, session_id: str, arg: str) -> dict:
    import httpx
    body = _ARG_TO_STATE.get(arg)
    if body is None:
        raise ValueError(arg)
    token = await token_provider()
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.put(f"/v1/agent/sessions/{session_id}/remote", json=body,
                        headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return (r.json() or {}).get("state", {})


def describe(state: dict) -> str:
    if not state.get("enabled"):
        return "Remote control: OFF."
    m = ", ".join(state.get("mirror", [])) or "—"
    s = ", ".join(state.get("steer", [])) or "—"
    return f"Remote control: ON · mirror → {m} · steer ← {s}"
