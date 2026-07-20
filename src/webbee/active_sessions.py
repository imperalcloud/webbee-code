"""Boot reattach notice (T6.3, coding-remote flow perfection): a best-effort
client for the gateway's own-user session-discovery endpoint, plus the pure
decision logic that turns its listing into 0-2 boot-time notes.

`fetch_active_sessions` is the house pattern used across this package
(sessions.py/thread.py): (cfg, token_provider), lazy httpx (or the repl's
shared keep-alive client), Bearer auth, and it never raises -- a listing
someone forgot to close, or a gateway that hasn't shipped this route yet,
must never become a boot-time crash or delay. `boot_reattach_notice` is pure
(no I/O) so a test drives it directly with fake session dicts."""
from __future__ import annotations


async def _get(cfg, client, path: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    if client is not None:
        r = await client.get(path, headers=headers)
        r.raise_for_status()
        return r
    import httpx
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        r = await c.get(path, headers=headers)
        r.raise_for_status()
        return r


async def fetch_active_sessions(cfg, token_provider, *, client=None) -> list[dict]:
    """This user's own Running coding/marathon sessions across every
    terminal/surface (gateway T2: `GET /v1/agent/sessions/active`, user JWT).
    Best-effort: ANY failure (network, auth, an older gateway without the
    route yet) returns [] rather than raising. `client=` reuses the repl's
    shared keep-alive AsyncClient; None falls back to a fresh per-call one
    (same convention as sessions.py/thread.py)."""
    try:
        token = await token_provider()
        r = await _get(cfg, client, "/v1/agent/sessions/active", token)
        return (r.json() or {}).get("sessions", [])
    except Exception:
        return []


def boot_reattach_notice(sessions: list[dict], repo_key: str) -> list[str]:
    """Decide what, if anything, to tell the user about their OTHER running
    sessions once THIS terminal's own boot replay has finished. `sessions`
    is the gateway's own-user listing above; `repo_key` is this terminal's
    own repo identity (webbee.repo.compute_repo_key) -- the gateway keys
    every session id `{kind}-{imperal_id}-r{repo_key}` (steer.derive_session_id),
    so a session belongs to THIS repo iff its id ends with `-r{repo_key}`.

    Returns 0-2 lines, rendered verbatim by the caller (one sink.note per
    line):
      * a session already Running in THIS repo -- a reattach note, plus (when
        it is parked on an approval) one more line pointing at the panel;
      * ANY session in another repo waiting on an approval -- ONE dim
        pointer, never repeated per session and never naming the repo/session
        id (no internals beyond what the panel already shows).

    Pure (no I/O, no imports) -- a test drives it directly with fake dicts."""
    suffix = f"-r{repo_key}"
    own = None
    other_parked = False
    for s in sessions:
        sid = str(s.get("session_id") or "")
        if sid.endswith(suffix):
            if own is None:
                own = s
        elif s.get("pending_consent"):
            other_parked = True

    lines = []
    if own is not None:
        lines.append("reattached to your running session — it keeps its history")
        if own.get("pending_consent"):
            lines.append("it is waiting for an approval — the prompt will re-show; "
                         "you can also approve from the panel")
    if other_parked:
        lines.append("you have a parked session waiting for approval in another repo "
                     "— open webbee there or approve from the panel")
    return lines
