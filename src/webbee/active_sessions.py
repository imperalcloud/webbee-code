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


def live_session_ids(sessions: list[dict]) -> set:
    """PURE. The set of session ids the gateway currently reports as RUNNING.
    One helper so the tab-restore reconciliation and the status indicator read
    liveness from the SAME shape (0.3.37)."""
    return {str(s.get("session_id") or "") for s in (sessions or [])
            if str(s.get("session_id") or "")}


def session_indicator(sessions: list[dict], repo_key: str) -> str:
    """PURE. The PERSISTENT live-session indicator for the toolbar (0.3.37).

    Before this, a running Temporal workflow was mentioned exactly once, in a
    boot note that scrolled away -- so after an accidental exit and reopen the
    terminal silently re-attached with nothing on screen ever confirming that
    it had seen a running session, or that one existed at all.

    Returns a SHORT string for the status line, or "" when there is nothing
    to say (no running sessions at all -- silence is correct then, the dock
    must not grow permanent noise):
      * this repo has a live session      -> "● live"
      * ...and it is parked on an approval -> "● live · needs approval"
      * only OTHER repos have live ones    -> "○ 2 elsewhere"
    Honest by construction: it reports what the gateway's Temporal listing
    actually says, never a guess.
    """
    suffix = f"-r{repo_key}"
    own, own_parked, others = False, False, 0
    for s in (sessions or []):
        sid = str(s.get("session_id") or "")
        if not sid:
            continue
        if sid.endswith(suffix):
            own = True
            if s.get("pending_consent"):
                own_parked = True
        else:
            others += 1
    if own:
        return "● live · needs approval" if own_parked else "● live"
    if others:
        return f"○ {others} elsewhere"
    return ""


def plan_tab_restore(saved: list, sessions: list[dict]) -> list:
    """PURE. Reconcile the REMEMBERED tab layout (tab_store.load_tabs) against
    the sessions Temporal reports as Running, and decide per tab what reopening
    it means (0.3.37).

    Each returned dict is the saved record plus:
      * `attach`: True  -> its workflow is STILL RUNNING; the tab must re-attach
                           and pick the live run up where it left off.
        `attach`: False -> nothing is running under that id any more; restore
                           the tab's state (label/mode/draft) only.
      * `pending_consent`: carried through from the live listing so a restored
        tab can immediately show it is parked on an approval.

    A saved tab with no session_id is legal (a tab you never sent anything in)
    and simply restores as state-only. Never raises; unknown/extra keys on the
    saved record are ignored rather than trusted.
    """
    live = {}
    for s in (sessions or []):
        sid = str(s.get("session_id") or "")
        if sid:
            live[sid] = s
    out = []
    for rec in (saved or []):
        if not isinstance(rec, dict):
            continue
        sid = str(rec.get("session_id") or "")
        s = live.get(sid)
        item = dict(rec)
        item["attach"] = bool(sid and s is not None)
        item["pending_consent"] = bool((s or {}).get("pending_consent"))
        out.append(item)
    return out


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
