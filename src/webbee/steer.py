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
    (session.build_coding_context -> repo.compute_repo_key). The boot-replay
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
                          interval: float = _POLL_INTERVAL) -> None:
    """Run forever (until cancelled): every ~`interval`s of idle time, drain
    the pending-steer queue and hand the FIRST item to `submit(text, surface)`
    -- the repl's normal turn path. Seams (all injected by the repl wiring):
      * is_busy()        -- sync; True while a turn is running. Checked BEFORE
                            the destructive fetch (never drain mid-turn) and
                            AGAIN right before submit (a locally-typed line
                            can win the race; the item goes back to the local
                            backlog instead of being lost).
      * live_session_id()-- sync; the agent's gateway-issued session id once a
                            turn has run ("" before). Wins over derivation.
      * submit(text, surface) -- async; renders the remote line and runs the
                            turn. Runs INSIDE this task, so polling is
                            naturally paused for the turn's whole duration."""
    from webbee.thread import fetch_pending_steer
    derived = ""
    backlog: deque = deque()
    while True:
        await asyncio.sleep(interval)
        try:
            if is_busy():
                continue
            if not backlog:
                sid = live_session_id()
                if not sid:
                    if not derived:
                        derived = await derive_session_id(
                            cfg, token_provider, workspace, marathon=marathon)
                    sid = derived
                backlog.extend(await fetch_pending_steer(cfg, token_provider, sid))
            if not backlog:
                continue
            item = backlog.popleft()
            text = str(item.get("text") or "").strip()
            if not text:
                continue  # malformed/blank item -- drop, never submit a no-op turn
            if is_busy():
                backlog.appendleft(item)  # a local line won the race -- defer, don't drop
                continue
            await submit(text, str(item.get("surface") or "telegram"))
            if _cancel_absorbed():
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-soft by design: a network blip / auth hiccup skips this
            # tick; undrained items stay durable on the gateway (1h TTL).
            continue
