import asyncio
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Account:
    signed_in: bool = False
    email: str = ""
    nickname: str = ""
    plan: str = ""
    plan_status: str = ""
    plan_renews: str = ""
    dev_tier: str = ""
    member_since: str = ""


def _fmt_month(iso: str) -> str:
    """'2026-04-27T10:00:00Z' -> 'Apr 2026'; '' on any failure."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %Y")
    except Exception:
        return ""


def _fmt_day(iso: str) -> str:
    """'2026-08-01T00:00:00Z' -> '2026-08-01'; '' on any failure."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return ""


async def _default_get(cfg, token: str, path: str) -> dict:
    import httpx
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=3.0) as c:
        r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


async def login_device_flow(cfg, auth, sink) -> str:
    """ONE shared imperal_mcp mechanism: device-code flow (RFC 8628), async,
    so the caller awaits it directly on the dock's event loop (the /login turn
    runs as a background task, so the dock stays responsive while it polls).
    on_prompt renders the code + URL into the feed — a bare print would be
    invisible in the dock. Returns the signed-in email."""
    def _login_prompt(user_code, uri, uri_complete):
        show = getattr(sink, "login_prompt", None)
        if show:
            show(user_code, uri)
        else:
            sink.note(f"Open {uri} and enter code: {user_code}")
    return await auth.login_device(cfg, on_prompt=_login_prompt)


async def fetch_account(cfg, token_provider, *, get=None) -> Account:
    """Best-effort account summary for the welcome screen. NEVER raises: no
    token or a failed /v1/auth/me -> Account(signed_in=False); a failed
    billing/developer call just omits those fields."""
    try:
        token = await token_provider()
    except Exception:
        return Account(signed_in=False)

    async def getter(path: str) -> dict:
        if get is not None:
            return await get(path)
        return await _default_get(cfg, token, path)

    async def _try(path):
        try:
            return await getter(path)
        except Exception:
            return None

    me, sub, dev = await asyncio.gather(
        _try("/v1/auth/me"), _try("/v1/billing/subscription"), _try("/v1/developer/profile"))
    if not me:
        return Account(signed_in=False)
    attrs = me.get("attributes") or {}
    sub = sub or {}
    dev = dev or {}
    return Account(
        signed_in=True,
        email=str(me.get("email", "") or ""),
        nickname=str(dev.get("nickname", "") or ""),
        plan=str(sub.get("plan", "") or ""),
        plan_status=str(sub.get("status", "") or ""),
        plan_renews=_fmt_day(str(sub.get("expires_at", "") or "")),
        dev_tier=str(dev.get("tier", "") or attrs.get("developer_tier", "") or ""),
        member_since=_fmt_month(str(me.get("created_at", "") or dev.get("registered_at", "") or "")),
    )
