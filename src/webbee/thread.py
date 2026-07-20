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
§B) -- the drain webbee.steer polls while the REPL is idle -- and the
mid-turn inject POST (0.3.15) the dock fires on Enter-while-busy."""
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


async def _request(cfg, client, method: str, path: str, *, token: str, json=None):
    """Shared HTTP leg for the three functions below. With a `client` (the
    repl's shared keep-alive AsyncClient) reuse it — no new TCP+TLS handshake.
    Without one, fall back to today's ephemeral per-call client (unchanged
    behavior for existing callers/tests, method-for-method: GET uses .get,
    everything else uses .post with the given json body)."""
    headers = {"Authorization": f"Bearer {token}"}
    if client is not None:
        r = await client.request(method, path, json=json, headers=headers)
        r.raise_for_status()
        return r
    import httpx
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
        if method == "GET":
            r = await c.get(path, headers=headers)
        else:
            r = await c.post(path, json=json, headers=headers)
        r.raise_for_status()
        return r


async def fetch_recent_thread(cfg, token_provider, session_id: str, *, client=None) -> list[dict]:
    """Recent tail of the durable per-user thread, for boot replay. `client=`
    reuses the repl's shared keep-alive client; None keeps the per-call
    client."""
    token = await token_provider()
    r = await _request(cfg, client, "GET", f"/v1/agent/sessions/{session_id}/thread", token=token)
    return (r.json() or {}).get("messages", [])


async def fetch_pending_steer(cfg, token_provider, session_id: str, *, client=None,
                              mode: str = "", label: str = "") -> dict:
    """Drain this user's pending-steer state (idle-steer pickup, liveness v2
    §B + full-queue-layer mode adoption) -- the /thread endpoint's sibling,
    same auth. Returns the gateway payload verbatim:
      * "items"          -- queued remote instructions. The gateway read is
                            DESTRUCTIVE: each item is returned exactly ONCE,
                            oldest first (empty when nothing is queued or
                            remote control is disabled), so the caller owns
                            every item it receives.
      * "requested_mode" -- one-shot remote mode request {mode, surface} or
                            null (GETDEL on the gateway -- delivered exactly
                            once; older gateways omit the key entirely).
      * "attach"         -- {task_id, last_id, kind} or null (attach-on-
                            poll): set ONLY when "items" above came back
                            empty AND this session's stream tail still
                            holds an unanswered tool_request/confirm_request
                            (a marathon turn woken elsewhere dispatched it
                            while this terminal sat idle). webbee.steer
                            hands it to the `attach_turn` seam; older
                            gateways omit the key entirely.
    `mode=` (T6.2, applied-mode report): the polled session's CURRENT
    coding mode, appended as `?mode={mode}` when non-empty -- the gateway
    stores it as `applied_mode` so the panel/TG can show the terminal's REAL
    mode instead of guessing. Omitted entirely when "" (default), so an
    older gateway that doesn't expect the query param sees the exact same
    request as before this feature.
    `label=` (W4c T3, label sync): the polled session's CURRENT tab title
    (auto-labeled or /rename'd), urlencoded and appended as `&label={label}`
    (or `?label=` alone when `mode` is absent) -- the gateway stores it under
    the SAME `imperal:coding_remote:label:{session_id}` key `/notify` already
    writes to, so the panel/TG picks up a self-named or renamed tab within
    one poll tick. Omitted entirely when "" (default), same back-compat
    posture as `mode`.
    Non-swallowing like fetch_recent_thread above: the poller (webbee.steer)
    wraps each tick in its own try/except. `client=` reuses the repl's shared
    keep-alive client; None keeps the per-call client."""
    token = await token_provider()
    path = f"/v1/agent/sessions/{session_id}/pending-steer"
    params = []
    if mode:
        params.append(f"mode={mode}")
    if label:
        from urllib.parse import quote
        params.append(f"label={quote(label)}")
    if params:
        path += "?" + "&".join(params)
    r = await _request(cfg, client, "GET", path, token=token)
    return r.json() or {}


async def inject_to_session(cfg, token_provider, session_id: str, text: str,
                            steer_iid: str, *, client=None) -> bool:
    """Mid-turn inject (0.3.15): POST an Enter-while-busy line straight into
    the user's OWN running session — `/v1/agent/sessions/{id}/inject`, body
    `{text, steer_iid}`. The gateway signals a task_id-LESS new_task, so the
    kernel's mid-turn fly-in absorbs it at the next brain step under the
    running turn's own task_id (frames stay visible in this terminal), and
    the given steer_iid rides the kernel's dedup ring. Returns True only when
    the gateway accepted it ({ok: true}). Non-swallowing like its siblings
    above — the repl wiring wraps it and falls back to the local type-ahead
    queue on any failure. `client=` reuses the repl's shared keep-alive
    client; None keeps the per-call client."""
    token = await token_provider()
    r = await _request(cfg, client, "POST", f"/v1/agent/sessions/{session_id}/inject",
                       token=token, json={"text": text, "steer_iid": steer_iid})
    return bool((r.json() or {}).get("ok"))


def truncate_for_display(text, limit: int = _DISPLAY_LIMIT) -> str:
    """Cap one replayed message's text so a single huge assistant reply can't
    flood the boot screen -- foreign_turn renders one line per message."""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
