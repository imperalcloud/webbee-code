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
                            marathon: bool = True, slot_id: str = "") -> str:
    """The REAL session id THIS terminal's turns use, derivable BEFORE any
    turn has run: the gateway keys coding sessions stable per user+repo as
    `{marathon|coding}-{imperal_id}-r{repo_key}` (agent_sessions/router.py)
    from the repo_key the client itself sends in coding_context
    (coding_context.build_coding_context -> repo.compute_repo_key). The boot-replay
    placeholder (`marathon-{iid}-rboot`) would pass the gateway's ownership
    prefix check but MISS the per-session remote-control state key
    (resolve_state reads k_state(session_id) -- set by /notify under the real
    id), so polling with it would read "disabled" and pickup would never
    fire.

    `slot_id` (W4b T5, additive) appends `-s{slot_id}` -- the SAME suffix the
    gateway's `StartRequest.slot` mints into the id server-side, so a LATER
    tab's poller derives its OWN session id (never the boot placeholder, and
    never tab-1's legacy id) even before that tab's first turn has run.
    Empty (tab-1 / the fallback loop's only slot) keeps today's id exactly."""
    from imperal_mcp.client import ImperalClient
    imperal_id = await ImperalClient(cfg, token_provider).whoami()

    def _repo_key() -> str:  # runs `git remote get-url` -- keep off the event loop
        from webbee.repo import compute_repo_key, find_repo_root
        return compute_repo_key(find_repo_root(workspace))

    prefix = "marathon" if marathon else "coding"
    sid = f"{prefix}-{imperal_id}-r{await asyncio.to_thread(_repo_key)}"
    return f"{sid}-s{slot_id}" if slot_id else sid


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
                          mode_getter=None,
                          label_getter=None,
                          interval: float = _POLL_INTERVAL,
                          client=None,
                          idle_after_s: float = 300.0,
                          idle_interval: float = 30.0,
                          slot_id: str = "",
                          initial_delay: float = 0.0,
                          attach_turn=None,
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
      * mode_getter()     -- sync, optional (T6.2, applied-mode report);
                            returns the CURRENT mode of the session actually
                            being polled (the repl passes a getter reading
                            THAT slot -- never blindly `slots.active()`,
                            which can be a different tab than the one `sid`
                            above resolves to). Read fresh every tick and
                            handed to fetch_pending_steer as `mode=`, so the
                            gateway's applied-mode record follows a local
                            mode change (Shift-Tab, /mode, a remote flip)
                            within ONE poll tick -- ~`interval` seconds later
                            at the fast cadence, no extra poke required.
                            None/"" omits the query param entirely (old
                            wiring, or no session polled yet).
      * label_getter()     -- sync, optional (W4c T3, label sync); returns
                            the CURRENT tab title of the session actually
                            being polled -- same "read fresh every tick,
                            never blindly slots.active()" discipline as
                            `mode_getter` above. Handed to
                            fetch_pending_steer as `label=`, which appends
                            `&label={label}` (urlencoded) so a self-named or
                            /rename'd tab reaches the gateway within one
                            poll tick. None/"" omits the query param
                            entirely, identical to `mode_getter` absent.
      * slot_id            -- W4b T5, optional; threaded verbatim into
                            `derive_session_id` so a LATER tab's poller
                            derives ITS OWN `-s{slot_id}` session id (never
                            tab-1's legacy id) even before that tab's first
                            turn has run and `live_session_id()` still
                            returns "". "" (tab-1 / fallback's only slot)
                            keeps today's derivation exactly.
      * initial_delay      -- W4b T5, optional; slept ONCE before the very
                            first tick, never again -- the repl staggers
                            each new per-slot poller's start (a small
                            incrementing offset) so several tabs opened
                            back-to-back don't all hit the gateway in the
                            same instant. 0.0 (default) skips the sleep
                            entirely -- byte-identical to before this param
                            existed.
      * attach_turn(attach)-- async, optional (attach-on-poll); called with
                            the fetch's `attach` field ({task_id, last_id,
                            kind} or falsy) -- set by the gateway ONLY when
                            the drain found no items AND the polled
                            session's stream tail holds an unanswered
                            tool_request/confirm_request (a marathon turn
                            woken elsewhere dispatched it while this
                            terminal sat idle, and nothing attached the
                            stream to let the kernel re-dispatch it). Same
                            discipline as `submit` above: runs INSIDE this
                            task (polling pauses for its whole duration),
                            and `is_busy()` is re-checked right before the
                            call -- a local turn winning the race meanwhile
                            simply defers (the gateway keeps holding the
                            pending request until it's answered, so the
                            next tick's fetch reports the SAME `attach`
                            again -- nothing is lost). None (old wiring, or
                            an older gateway that never sends `attach`) is a
                            silent no-op, byte-identical to before this
                            param existed.

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
    if initial_delay:
        await asyncio.sleep(initial_delay)
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
                            cfg, token_provider, workspace, marathon=marathon,
                            slot_id=slot_id)
                    sid = derived
                # Old-style test doubles for fetch_pending_steer don't accept
                # a client/mode kwarg -- only pass either when the repl
                # actually gave us one, so back-compat call sites stay
                # untouched.
                fetch_kw = {"client": client} if client is not None else {}
                mode = mode_getter() if mode_getter is not None else ""
                if mode:
                    fetch_kw["mode"] = mode
                label = label_getter() if label_getter is not None else ""
                if label:
                    fetch_kw["label"] = label
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
                attach = payload.get("attach")
                if attach and attach_turn is not None and not backlog:
                    # Attach-on-poll: the drain found NO items, but the
                    # gateway says THIS session's stream tail is still
                    # holding an unanswered request -- re-check is_busy
                    # right before, same discipline as the pre-submit race
                    # below (a local line/turn can win meanwhile; deferring
                    # loses nothing, the gateway keeps reporting the SAME
                    # `attach` until it's actually answered).
                    if is_busy():
                        continue
                    last_active = now()  # an attach pickup = activity
                    await attach_turn(attach)
                    if _cancel_absorbed():
                        raise asyncio.CancelledError
                    failures = 0
                    continue
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
