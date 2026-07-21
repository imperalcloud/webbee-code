"""Wallet client — this account's credits balance, cap and plan status, read
from the gateway for Home's Wallet tile. Best-effort, house pattern
(account.py/sessions.py/remote.py): (cfg, token_provider), lazy httpx, Bearer
auth, a bounded 3s timeout, and it NEVER raises -- ANY failure (no token, a
402 Payment Required, a non-200, a timeout, an older gateway without the
route) returns None so the tile shows a neutral placeholder rather than
crashing or blocking Home's boot. User-facing term for `balance` is
"credits"; the internal/API name is tokens."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Wallet:
    balance: int = 0
    cap: int = 0
    plan: str = ""
    status: str = ""
    included_tokens: int = 0


async def _default_get(cfg, token: str, path: str) -> dict:
    import httpx
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=3.0) as c:
        r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


async def fetch_wallet(cfg, token_provider, *, get=None) -> "Wallet | None":
    """The account's live wallet, or None on ANY failure (see module doc).
    `get=` is a DI seam for tests (mirrors account.fetch_account): an async
    callable `get(path) -> dict` that raises on a non-200/402 -- production
    resolves the token then hits GET /v1/billing/wallet."""
    try:
        token = await token_provider()
    except Exception:
        return None

    async def getter(path: str) -> dict:
        if get is not None:
            return await get(path)
        return await _default_get(cfg, token, path)

    try:
        data = await getter("/v1/billing/wallet")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return Wallet(
        balance=_int(data.get("balance")),
        cap=_int(data.get("cap")),
        plan=str(data.get("plan", "") or ""),
        status=str(data.get("status", "") or ""),
        included_tokens=_int(data.get("included_tokens")),
    )
