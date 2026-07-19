"""Idle-steer pickup (terminal liveness v2 §B): while the REPL sits idle at
the input prompt, a background task polls the gateway's pending-steer queue
-- instructions sent from Telegram/panel while no turn was running to steer
into -- and runs the oldest one through the NORMAL turn path, rendered and
tagged as the remote user's line. The terminal stays the sole executor.

Drain contract: the gateway returns each queued item exactly ONCE (LPUSH'd
by inject_instruction, RPOP-drained oldest-first by /pending-steer), so a
multi-item drain is buffered locally and fed to the turn path one item per
idle tick -- ordering preserved, nothing lost, and a turn started by one
item naturally pauses the poll until it ends.

Fail-soft everywhere: any error (network blip, auth hiccup, whoami) skips
the tick and the next one retries -- undelivered instructions stay durable
on the gateway (1h TTL), so silence never loses anything."""
from __future__ import annotations

import asyncio
from collections import deque

_POLL_INTERVAL = 4.0


async def derive_session_id(cfg, token_provider, workspace: str, *,
                            marathon: bool = True) -> str:
    """The REAL session id THIS terminal's turns use, derivable BEFORE any
    turn has run: the gateway keys coding sessions stable per user+repo as
    `{marathon|coding}-{imperal_id}-r{repo_key}` (agent_sessions/router.py)
    from the repo_key the client itself sends in coding_context
    (coding_context.build_coding_context -> repo.compute_repo_key). The boot-replay
    placeholder (`marathon-{iid}-rboot`) would pass the gateway's ownership
    prefix check but MISS the per-session remote-control state key
    (resolve_state reads k_state(session_id) -- set by /notify under the real
    id), so polling with it would read "disabled" and pickup would never
    fire."""
    from imperal_mcp.client import ImperalClient
    imperal_id = await ImperalClient(cfg, token_provider).whoami()

    def _repo_key() -> str:  # runs `git remote get-url` -- keep off the event loop
        from webbee.repo import compute_repo_key, find_repo_root
        return compute_repo_key(find_repo_root(workspace))

    prefix = "marathon" if marathon else "coding"
    return f"{prefix}-{imperal_id}-r{await asyncio.to_thread(_repo_key)}"


def _consume_mode(payload, on_mode) -> bool:
    """Hand the fetch's one-shot `requested_mode` ({mode, surface} -- GETDEL
    on the gateway, delivered exactly once) to the injected
    `on_mode(mode, surface)` seam. Fail-soft and guarded like every other
    seam: a missing/malformed field, an old gateway (no key) or a seam error
    never kills the poller -- a lost mode flip is safe (the mode simply
    stays), a dead poller is not. Returns True when a request was handed
    off, so the caller can yield once and let a just-spawned local confirm
    (the autopilot upgrade prompt) arm before any queued item submits."""
    if on_mode is None or not isinstance(payload, dict):
        return False
    req = payload.get("requested_mode")
    if not isinstance(req, dict):
        return False
    mode = str(req.get("mode") or "").strip().lower()
    if not mode:
        return False
    try:
        on_mode(mode, str(req.get("surface") or "").strip() or "remote")
    except Exception:
        return False
    return True


def _cancel_absorbed() -> bool:
    """True when this task received a cancel that a submitted turn swallowed
    (repl._run_turn treats CancelledError as a user interrupt). Without this
    check the poller would sail past its own cancellation and asyncio.run's
    shutdown would hang on a task that ignored it."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def poll_idle_steer(cfg, token_provider, *, workspace: str, is_busy,
                          submit, marathon: bool = True,
                          live_session_id=lambda: "",
                          on_mode=None,
                          interval: float = _POLL_INTERVAL,
                          client=None,
                          idle_after_s: float = 300.0,
                          idle_interval: float = 30.0,
                          _monotonic=None) -> None:
    """Run forever (until cancelled): every ~`interval`s of idle time, drain
    the pending-steer queue and hand the FIRST item to
    `submit(text, surface, steer_iid)` -- the repl's normal turn path. Seams
    (all injected by the repl wiring):
      * is_busy()        -- sync; True while a turn is running. Checked BEFORE
                            the destructive fetch (never drain mid-turn) and
                            AGAIN right before submit (a locally-typed line
                            can win the race; the item goes back to the local
                            backlog instead of being lost).
      * live_session_id()-- sync; the agent's gateway-issued session id once a
                            turn has run ("" before). Wins over derivation.
      * submit(text, surface, steer_iid) -- async; renders the remote line and
                            runs the turn. Runs INSIDE this task, so polling
                            is naturally paused for the turn's whole duration.
                            steer_iid = the queued item's dedup id ("" on an
                            older gateway), threaded into the turn POST so the
                            kernel's dedup ring can drop an at-least-once twin.
      * on_mode(mode, surface) -- sync, optional; receives the payload's
                            one-shot `requested_mode` (a coding-mode flip
                            asked from TG/panel) BEFORE any fetched item is
                            submitted, so the flip governs the turn it rode
                            in with. Must never block the poller: the repl's
                            wiring applies downgrades instantly and spawns
                            the autopilot local-confirm as its own task.
      * client            -- the repl's shared keep-alive AsyncClient (Task
                            12), threaded into fetch_pending_steer so an idle
                            terminal stops opening a fresh TCP+TLS handshake
                            every tick, forever. None (tests / no repl-owned
                            client) keeps today's per-call client.

    Adaptive cadence (Task 12): after `idle_after_s` (default 5 minutes)
    without activity, the tick relaxes from `interval` (4s) to
    `idle_interval` (30s) -- an idle terminal stops hammering the gateway.
    Activity (a busy tick, a successful fetch that returned items, or a
    submitted item) resets the clock back to the fast cadence. The failure
    backoff multiplier applies to whichever base is active. `_monotonic` is a
    test seam (defaults to time.monotonic)."""
    from webbee.thread import fetch_pending_steer
    import time
    now = _monotonic or time.monotonic
    derived = ""
    backlog: deque = deque()
    failures = 0    # consecutive fetch/auth failures -> backoff (a logged-out
                    # terminal must not hammer the token-refresh path every 4s)
    last_active = now()
    while True:
        base = interval if (now() - last_active) < idle_after_s else idle_interval
        await asyncio.sleep(min(base * (2 ** min(failures, 4)), 60.0))
        try:
            if is_busy():
                last_active = now()      # a running turn = activity
                continue
            if not backlog:
                sid = live_session_id()
                if not sid:
                    if not derived:
                        derived = await derive_session_id(
                            cfg, token_provider, workspace, marathon=marathon)
                    sid = derived
                # Old-style test doubles for fetch_pending_steer don't accept
                # a client kwarg -- only pass it when the repl actually gave
                # us one, so back-compat call sites stay untouched.
                fetch_kw = {"client": client} if client is not None else {}
                payload = await fetch_pending_steer(cfg, token_provider, sid, **fetch_kw) or {}
                if payload.get("items"):
                    last_active = now()  # a successful drain = activity
                backlog.extend(payload.get("items") or [])
                if _consume_mode(payload, on_mode):
                    # Yield ONE loop cycle so a just-spawned local confirm
                    # (autopilot upgrade) arms its prompt before the
                    # pre-submit busy re-check below -- a fetched item then
                    # defers until the person at the terminal has answered.
                    await asyncio.sleep(0)
            if not backlog:
                continue
            item = backlog.popleft()
            text = str(item.get("text") or "").strip()
            if not text:
                continue  # malformed/blank item -- drop, never submit a no-op turn
            if is_busy():
                backlog.appendleft(item)  # a local line won the race -- defer, don't drop
                continue
            last_active = now()          # an item arrived = activity
            await submit(text, str(item.get("surface") or "telegram"),
                         str(item.get("iid") or ""))
            if _cancel_absorbed():
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-soft by design: a network blip / auth hiccup skips this
            # tick; undrained items stay durable on the gateway (1h TTL).
            # Consecutive failures back the poll off (up to 60s) so a
            # logged-out terminal never hammers the token-refresh path.
            failures += 1
            continue
        failures = 0
