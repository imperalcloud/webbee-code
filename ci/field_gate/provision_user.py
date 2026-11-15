"""Provision the field gate's test user through the gateway's own doors, and log it in under its own HOME.

    python -m ci.field_gate.provision_user [--email gate@imperal.io] [--home ~/.imperal-gate]

Owner 2026-11-15: the gate uses one dedicated test user, always. The admin is whoever runs this, by their normal
imperal-mcp login (their ~/.imperal/credentials.json); nothing secret lives in this file. The user is created by the
admin door (POST /v1/users: audited, account-integrity signed -- never by SQL), logged in by POST /v1/auth/login, and
its tokens are written to <home>/.imperal/credentials.json in imperal-mcp's own format. The one-time password is
random and never stored: when the login ever lapses, run this again -- an existing user gets a new password through
the same admin door.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

DEFAULT_EMAIL = "gate@imperal.io"


async def _admin_token() -> tuple:
    from imperal_mcp.auth import ensure_access_token
    from imperal_mcp.config import Config
    cfg = Config.from_env()
    return cfg, await ensure_access_token(cfg)


async def register_developer(cli, api_url: str, access_token: str) -> None:
    """The app tools (list_apps, run_read_tool, deploy_ir) need a developer: the free tier, by the user's own door
    (POST /v1/developer/register; explorer costs 0). Already registered is fine."""
    r = await cli.post(f"{api_url}/v1/developer/register", json={"tier": "explorer", "nickname": "field-gate"},
                       headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code >= 400 and "already" not in r.text.lower():
        r.raise_for_status()


async def provision(email: str, home: Path) -> str:
    import httpx
    cfg, admin = await _admin_token()
    password = secrets.token_urlsafe(32)
    head = {"Authorization": f"Bearer {admin}"}
    async with httpx.AsyncClient(timeout=30) as cli:
        made = await cli.post(f"{cfg.api_url}/v1/users", headers=head,
                              json={"email": email, "password": password, "role": "user",
                                    "attributes": {"email_verified": True, "purpose": "field-gate test user"}})
        if made.status_code == 409:
            found = await cli.get(f"{cfg.api_url}/v1/users", headers=head, params={"search": email, "limit": 5})
            found.raise_for_status()
            user = next((u for u in found.json().get("items", []) if u.get("email") == email), None)
            if user is None:
                raise SystemExit(f"{email} exists but the admin list does not show it")
            # the admin door has no password field: deactivate, then create again -- POST /v1/users reactivates an
            # inactive user with the new password (users/router.py create_user, the "existing" branch)
            off = await cli.patch(f"{cfg.api_url}/v1/users/{user['imperal_id']}", headers=head,
                                  json={"is_active": False})
            off.raise_for_status()
            again = await cli.post(f"{cfg.api_url}/v1/users", headers=head,
                                   json={"email": email, "password": password, "role": "user"})
            again.raise_for_status()
        else:
            made.raise_for_status()
        tok = await cli.post(f"{cfg.api_url}/v1/auth/login", json={"email": email, "password": password})
        tok.raise_for_status()
        t = tok.json()
        await register_developer(cli, cfg.api_url, t["access_token"])
    creds_dir = home / ".imperal"
    creds_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(creds_dir, 0o700)
    path = creds_dir / "credentials.json"
    path.write_text(json.dumps({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                                "access_expires_at": time.time() + int(t.get("expires_in", 900))}))
    os.chmod(path, 0o600)
    return str(path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--email", default=DEFAULT_EMAIL)
    p.add_argument("--home", default=str(Path.home() / ".imperal-gate"))
    a = p.parse_args()
    print(asyncio.run(provision(a.email, Path(a.home).expanduser())))
    sys.exit(0)


if __name__ == "__main__":
    main()
