"""Forget everything the vault holds for the field gate's test user -- through the one memory door, nothing else.

    IMPERAL_GATE_HOME=~/.imperal-gate python -m ci.field_gate.wipe

POST /v1/memory/forget {"what": "all"} as the test user itself (its JWT): the gateway forgets, then checks, and a
forget that leaves anything behind answers 409 naming what is left (gateway app/memory/router.py). The login is
refreshed in place (imperal-mcp's own credentials handling under the gate HOME).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from ci.field_gate.provision_user import DEFAULT_EMAIL as GATE_EMAIL


async def wipe() -> dict:
    import httpx
    from imperal_mcp.auth import ensure_access_token
    from imperal_mcp.config import Config
    cfg = Config.from_env()
    token = await ensure_access_token(cfg)
    async with httpx.AsyncClient(timeout=120) as cli:
        me = await cli.get(f"{cfg.api_url}/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        me.raise_for_status()
        who = me.json().get("email")
        if who != GATE_EMAIL:          # never anyone else's memory: a wrong HOME would forget a real person's
            raise SystemExit(f"refused: the login under this HOME is {who!r}, not the gate user {GATE_EMAIL}")
        r = await cli.post(f"{cfg.api_url}/v1/memory/forget", json={"what": "all"},
                           headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 409:
            raise SystemExit(f"forget left something behind: {r.text[:500]}")
        r.raise_for_status()
        return r.json()


def main() -> None:
    gate = os.environ.get("IMPERAL_GATE_HOME")
    if not gate:
        raise SystemExit("IMPERAL_GATE_HOME: the HOME holding the test user's login")
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(Path(gate).expanduser())
    print(asyncio.run(wipe()))
    sys.exit(0)


if __name__ == "__main__":
    main()
