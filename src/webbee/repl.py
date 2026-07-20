import asyncio
import contextlib
import os
import sys

from webbee import __version__, boot
from webbee.account import login_device_flow
from webbee.commands import CommandContext, dispatch
from webbee.session import AgentSession
from webbee.slots import SessionSlot, SlotManager, WorkspaceResources, close_active
from webbee.tui import _MODES, next_mode


async def run_marathon(cfg, mode: str, goal: str, *, sink=None, auth=None,
                       agent_factory=None) -> str:
    """Launch ONE autonomous marathon toward `goal` and stream it to stdout.

    A marathon reuses the whole coding path — same AgentSession, same reconnecting
    stream reader — it just flags the request `marathon=True` (routing it to the
    kernel MarathonWorkflow) and lets AgentSession attach the CLIENT-detected
    verify_cmd. Non-dock (streams to a plain sink) so a headless / CI launch works;
    the coding REPL (run_repl) is untouched."""
    if auth is None:
        from imperal_mcp import auth as _auth
        auth = _auth
    if sink is None:
        from webbee.render import RichSink
        sink = RichSink()

    workspace = os.getcwd()

    from webbee.tokens import make_token_provider
    token_provider = make_token_provider(cfg, auth)

    if agent_factory is None:
        agent_factory = lambda c, tp, ws, m: AgentSession(c, tp, ws, m)  # noqa: E731
    agent = agent_factory(cfg, token_provider, workspace, mode)

    sink.note(f"🏁 Marathon launched: {goal}")
    sink.begin_turn()
    try:
        text = await agent.run(goal, sink, marathon=True, goal=goal)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await agent.stop()
        sink.note("Interrupted.")
        sink.end_turn("")   # clear busy (poller starvation guard)
        return ""
    except Exception as e:  # network/auth/etc — never crash
        if type(e).__name__ in ("StreamAuthError", "NotLoggedInError"):
            sink.note("Session expired or access revoked — run /login to sign in again.")
        else:
            sink.note(f"Error: {type(e).__name__}: {e}")
        sink.end_turn("")   # clear busy: a stuck 'working' also starves the idle-steer poller
        return ""
    sink.end_turn(text)
    return text


async def _inject_via_gateway(cfg, token_provider, agent, sink,
                              text: str, steer_iid: str, client=None) -> bool:
    """The gateway leg of the dock's Enter-while-busy fly-in (mid-turn inject,
    0.3.15): POST the line straight into the agent's LIVE running session so
    the marathon absorbs it at the next brain step (seconds), instead of
    holding it client-side until the turn ends. On ok the line is kernel-owned
    — the ❯ echo records it as sent and the kernel's task_queued[terminal]
    echo drives the panel row. False on ANY failure (no live session yet,
    network, auth, gateway refusal) — the dock then falls back to today's
    local type-ahead queue (tui._inject_or_queue), carrying the same iid so
    the kernel ring dedups a twin. Module-level so tests drive it directly.
    `client=` (Task 12) reuses the repl's shared keep-alive AsyncClient; None
    keeps the per-call client (existing direct tests of this function)."""
    sid = getattr(agent, "session_id", "")
    if not sid:
        return False
    try:
        from webbee.thread import inject_to_session
        # Old-style test doubles for inject_to_session don't accept a client
        # kwarg -- only pass it when the repl actually gave us one.
        inject_kw = {"client": client} if client is not None else {}
        ok = await inject_to_session(cfg, token_provider, sid, text, steer_iid, **inject_kw)
    except Exception:
        return False
    if ok:
        sink.user_echo(text)   # the transcript records the message as sent
    return ok


def _gate_busy(sink, turn_ref: dict) -> bool:
    """Pure predicate behind `_poller_busy` (module-level so tests drive it
    directly, unlike the run_repl closure) -- LOCKOUT-PROOF like
    tui._busy_live: busy counts only while the turn TASK recorded in
    `turn_ref` is genuinely alive. A BaseException-class escape (or a raise
    inside end_turn) that leaves the sink's _busy flag stuck must no longer
    starve the idle-steer poller. `turn_ref` is populated ONLY on the dock
    path (the SAME dict object shared into tui.run_session); the fallback
    loop leaves it at {"task": None} forever, so the raw flag governs there
    (its end_turn paths are deterministic)."""
    busy = bool(getattr(sink, "is_busy", None) and sink.is_busy())
    t = turn_ref.get("task")
    if busy and t is not None and t.done():
        busy = False
    if busy:
        return True
    cp = getattr(sink, "consent_pending", None)
    return bool(cp and cp())


def _slot_ctx(slot: SessionSlot, *, logged_in: bool) -> CommandContext:
    """Pure extraction of the ACTIVE slot's fields into a CommandContext
    (W4a boot split, map §6): `state["mode"]`/`state["git_branch"]` are gone
    -- mode/git_branch/the type-ahead queue now live on the slot. Module-level
    so a test can drive slot-switching directly (build a SlotManager, flip
    active_idx, assert the fields follow) without running the whole REPL."""
    sink = slot.sink
    return CommandContext(mode=slot.mode, workspace=slot.workspace, version=__version__,
                          surface="terminal", logged_in=logged_in,
                          session_tokens=getattr(sink, "session_tokens", 0),
                          session_credits=getattr(sink, "session_credits", 0),
                          git_branch=slot.git_branch, queued=tuple(slot.pending))


def _live_session_id(slots: SlotManager) -> str:
    """The idle-steer poller's live-session seam (map §1): the ACTIVE slot's
    agent session id, or "" with no crash when the active slot has no agent
    at all (Home)."""
    agent = slots.active().agent
    return getattr(agent, "session_id", "") if agent is not None else ""


def _cancel_slot(slot: SessionSlot) -> None:
    """The tab-close flow's CLIENT-side teardown (Task 5) — the `cancel_slot`
    callable `webbee.slots.close_active` invokes on the slot it just removed
    from the SlotManager. Cancels this slot's own running turn (if any --
    the actual `_run_turn` background task tui.py started, tracked in
    `slot.turn["task"]`) plus anything parked in `slot.bg_tasks`, each guarded
    by `.done()` so an already-finished task is never double-cancelled.
    Does NOT touch the server-side run at all -- browser-tab model, per the
    wiring map: the kernel's MarathonWorkflow keeps going: only the local
    await/stream-read this PROCESS was doing for the tab dies here, so `/new`
    against the same repo later re-attaches to a run that never stopped."""
    task = slot.turn.get("task")
    if task is not None and not task.done():
        task.cancel()
    for t in slot.bg_tasks:
        if t is not None and not t.done():
            t.cancel()


async def _resources_bundle(cfg, workspace: str, resources: WorkspaceResources,
                            intel_factory, shadow_factory) -> dict:
    """Per-WORKSPACE boot phase (map §6): a cache hit means another slot on
    this SAME repo root already booted intel/shadow/git_branch -- share that
    bundle verbatim (same intel instance -- one watcher, one index, one
    reversibility shadow per repo). A miss boots it once (webbee.boot.
    boot_workspace) and caches the result for every later slot on this root."""
    bundle = resources.get(workspace)
    if bundle is None:
        bundle = await boot.boot_workspace(cfg, workspace, intel_factory, shadow_factory)
        resources.put(workspace, bundle)
    return bundle


def _resolve_agent_factory(agent_factory, bundle: dict):
    """The DEFAULT AgentSession factory must capture THIS bundle's intel/
    shadow (map §1 -- no more nonlocal intel/shadow singletons shared by every
    slot regardless of workspace). A caller-supplied factory (every existing
    test in this file) is used verbatim and simply ignores the bundle, same
    as before the split."""
    if agent_factory is not None:
        return agent_factory
    return lambda c, tp, ws, m: AgentSession(c, tp, ws, m, intel=bundle["intel"], shadow=bundle["shadow"])


async def _finish_slot(cfg, token_provider, workspace, mode, *, resources: WorkspaceResources,
                       agent_factory, intel_factory, shadow_factory, pane, sink, first: bool,
                       account) -> SessionSlot:
    """Shared tail of slot construction -- the dock's `_make_session_slot` AND
    the headless fallback loop both fall into this once they have their own
    pane (or None, fallback has no dock) + sink: resolve/boot the per-
    WORKSPACE resources bundle, build the agent, wire the sink's local queue
    to THIS slot's own deque, and -- gated by `first` (map §6 replay
    landmine) -- show the welcome banner and replay the durable thread. ONLY
    the very first session slot the process ever creates does either; every
    later tab (first=False) skips both."""
    bundle = await _resources_bundle(cfg, workspace, resources, intel_factory, shadow_factory)
    factory = _resolve_agent_factory(agent_factory, bundle)
    agent = factory(cfg, token_provider, workspace, mode)
    label = os.path.basename(os.path.normpath(workspace)) or workspace
    slot = SessionSlot(kind="session", workspace=workspace, label=label, pane=pane,
                       sink=sink, agent=agent, mode=mode, git_branch=bundle["git_branch"])
    # Queue-panel single-source dedup (0.3.16): hand the sink the SAME
    # type-ahead deque tui mutates for THIS slot, so a kernel task_queued
    # echo can promote a landed local twin (matched by steer_iid) into the
    # one kernel-owned row. Reference share — never a copy.
    sink.local_pending = slot.pending
    if first:
        if account is not None:
            sink.welcome(account, workspace, "terminal")
        # Boot replay of the durable per-user thread (Task 9) — best-effort,
        # never a boot blocker (webbee.boot.replay_thread swallows
        # everything). Landmine (map §6): replay is keyed by a per-USER
        # placeholder, not per-workspace -- a second slot replaying it would
        # show the wrong (or duplicate) history, so only the first session
        # slot the process ever creates gets it.
        await boot.replay_thread(cfg, token_provider, sink)
    return slot


async def _make_session_slot(cfg, token_provider, workspace, mode, *, resources: WorkspaceResources,
                             shared_client, agent_factory, intel_factory, shadow_factory,
                             first: bool, account=None) -> SessionSlot:
    """Builds ONE dock tab's atomic {agent, sink, pane} triple (map §6 —
    created together, a sink must never point at another slot's pane/
    console). `shared_client` isn't consumed here yet — reserved for Task 3's
    per-slot inject/steer wiring; accepted here for interface parity with the
    seams this factory feeds."""
    from webbee import tui
    from webbee.render import RichSink
    from webbee.sizing import get_size

    width, _height = get_size(None)   # pre-app: same fallback tui.run_session's own sizing uses
    pane = tui.OutputPane(width=width)
    sink = RichSink(console=pane.console, on_output=pane.notify)
    return await _finish_slot(cfg, token_provider, workspace, mode, resources=resources,
                              agent_factory=agent_factory, intel_factory=intel_factory,
                              shadow_factory=shadow_factory, pane=pane, sink=sink,
                              first=first, account=account)


async def run_repl(cfg, mode: str = "default", *, once: bool = False, sink=None, read_line=input,
                   agent_factory=None, auth=None, account_fetcher=None,
                   sessions_client=None, intel_factory=None, shadow_factory=None) -> None:
    """Interactive coding REPL. Production (a real tty, no injected sink) runs
    the persistent prompt_toolkit dock (`tui.run_session`): the bordered input
    box is pinned at the bottom, turn output scrolls above it (patch_stdout →
    native scrollback), and turns run as background tasks. Tests / non-tty use
    the injected sync `read_line` fallback loop. Both share `_handle`.

    W4a boot split (map §6): three phases. PROCESS-wide (once, regardless of
    how many slots ever exist) — the shared keep-alive client, the account
    fetch. Per-WORKSPACE (shared by same-repo slots, cached in
    `WorkspaceResources`) — intel + its watcher, the reversibility shadow,
    the cached git branch. Per-SLOT (`_make_session_slot`/`_finish_slot`) —
    the agent, the sink/pane, and — ONLY for the very first session slot —
    the welcome banner + thread replay. The dock path runs a Home slot
    (pane-only; Task 6 fills its content) alongside the first session slot;
    the fallback (non-dock) path stays single-slot with NO Home, so every
    existing fallback-path test keeps its world unchanged."""
    if auth is None:
        from imperal_mcp import auth as _auth
        auth = _auth
    if account_fetcher is None:
        from webbee.account import fetch_account as account_fetcher
    if sessions_client is None:
        from webbee import sessions as sessions_client

    workspace = os.getcwd()

    from webbee.tokens import make_token_provider
    token_provider = make_token_provider(cfg, auth)

    from webbee import http as _http
    # Task 12: the repl-lifetime keep-alive client — ONE AsyncClient for the
    # poller/inject/thread-replay calls instead of a fresh TCP+TLS handshake
    # per call. Process-wide (map §6): created once regardless of how many
    # slots the run ever has.
    shared_client = _http.make_client(cfg)

    # Prod dock path = the default reader + a real tty + no injected sink; tests
    # inject sink/read_line and take the plain fallback loop.
    use_dock = sink is None and read_line is input and sys.stdin.isatty()
    # W4a: `state` keeps only what is genuinely process-wide -- mode,
    # git_branch and the type-ahead queue now live on the SessionSlot
    # (map §6). `sessions` (the last /sessions listing) is added lazily.
    state = {"logged_in": False}
    slots = SlotManager()
    resources = WorkspaceResources()   # per-workspace boot cache (map §6)
    steer_task = None   # assigned by _spawn_steer -- idle-steer poller (webbee.steer), cancelled on exit
    # Task 5 (map contract item 5): the dock fills this at construction time
    # with `switch`/`close` -- the SAME `_switch_to`/`_close_flow` a click or
    # a key goes through (history swap, close note) -- so /tab, /new and
    # /close route through it too instead of mutating `slots` blind. Stays
    # `{}` forever on the fallback (non-dock) path -- `.get(..., default)`
    # below then falls back to a plain SlotManager call with no history/UI
    # side effects, which is exactly right where there is no dock at all.
    ui_hooks: dict = {}

    # Process-wide boot phase: ONE account fetch regardless of which slot(s)
    # end up existing.
    account = await account_fetcher(cfg, token_provider)
    state["logged_in"] = account.signed_in

    def _resources_for(ws: str) -> dict:
        return resources.get(ws) or {}

    def _cycle() -> None:
        slot = slots.active()
        slot.mode = next_mode(slot.mode)
        if slot.agent is not None:
            slot.agent.mode = slot.mode

    def _ctx() -> CommandContext:
        return _slot_ctx(slots.active(), logged_in=state["logged_in"])

    async def _handle(line: str) -> str:
        """Process one input line. Returns 'exit' or 'continue'."""
        if not line.strip():
            return "continue"
        res = dispatch(line, _ctx())
        if res.handled:
            slot = slots.active()
            _sink = slot.sink
            if res.exit:
                return "exit"
            if res.action == "login":
                # Device-code flow (RFC 8628) — rendering + polling in webbee.account.
                email = await login_device_flow(cfg, auth, _sink)
                state["logged_in"] = True
                _sink.note(f"Signed in as {email}.")
                return "continue"
            if res.action == "logout":
                await auth.logout(cfg)
                state["logged_in"] = False
                _sink.note("Signed out, local credentials removed.")
                return "continue"
            if res.action == "sessions":
                rows = await sessions_client.list_sessions(cfg, token_provider)
                state["sessions"] = rows
                _sink.sessions_table(rows)
                return "continue"
            if res.action == "sessions_revoke":
                rows = state.get("sessions") or []
                try:
                    idx = int(res.arg) - 1
                except ValueError:
                    idx = -1
                if idx < 0 or idx >= len(rows):
                    _sink.note("Usage: /sessions revoke <#> — run /sessions first to see the list.")
                    return "continue"
                s = rows[idx]
                if s.get("current"):
                    _sink.note("That's this terminal — use /logout to sign out here.")
                    return "continue"
                ok = await sessions_client.revoke_session(cfg, token_provider, s["session_id"])
                _sink.note(f"Revoked {s.get('label') or s.get('surface')}." if ok else "Failed to revoke session.")
                return "continue"
            if res.action == "logout_others":
                n = await sessions_client.revoke_others(cfg, token_provider)
                _sink.note(f"Signed out {n} other session(s)." if n >= 0 else "Failed to sign out other sessions.")
                return "continue"
            if res.action == "steps":
                from webbee.details import format_steps
                _sink.note(format_steps(getattr(slot.agent, "steps", [])))
                return "continue"
            if res.action == "step_detail":
                from webbee.details import build_step_ref, fetch_step_detail, format_steps
                _steps = getattr(slot.agent, "steps", [])
                try:
                    _idx = int(res.arg) - 1
                    _step = _steps[_idx]
                except (ValueError, IndexError):
                    _sink.note(f"No such step. {format_steps(_steps)}")
                    return "continue"
                if not _step.get("step_id") or not getattr(slot.agent, "session_id", ""):
                    _sink.note("No detail ref for this step.")
                    return "continue"
                _detail = await fetch_step_detail(
                    cfg, token_provider, build_step_ref(slot.agent.session_id, _step["step_id"]))
                if _detail:
                    _sink.step_detail(_detail)
                else:
                    _sink.note("Detail unavailable (expired or not recorded).")
                return "continue"
            if res.action == "checkpoints":
                shadow = _resources_for(slot.workspace).get("shadow")
                if shadow is None:
                    _sink.note("Reversibility is off (git unavailable).")
                else:
                    _sink.note(await asyncio.to_thread(shadow.describe))
                return "continue"
            if res.action == "rollback":
                shadow = _resources_for(slot.workspace).get("shadow")
                if shadow is None:
                    _sink.note("Reversibility is off (git unavailable).")
                elif not res.arg:
                    _sink.note("Usage: /rollback <id|cp-N|N>  (see /checkpoints)")
                else:
                    _r = await asyncio.to_thread(shadow.rollback, res.arg)
                    _sink.note(str(_r.get("content", "")))
                return "continue"
            if res.action == "notify":
                from webbee import remote as _remote
                sid = getattr(slot.agent, "session_id", "")
                if not sid:
                    _sink.note("Start a coding turn first, then /notify to route it.")
                    return "continue"
                try:
                    if res.arg in ("tg", "panel", "both", "off"):
                        st = await _remote.set_remote(cfg, token_provider, sid, res.arg)
                    elif res.arg:
                        _sink.note("Usage: /notify [tg|panel|both|off]")
                        return "continue"
                    else:
                        st = await _remote.get_remote(cfg, token_provider, sid)
                    _sink.note(_remote.describe(st))
                except Exception as e:
                    _sink.note(f"Remote control unavailable: {type(e).__name__}")
                return "continue"
            if res.action == "new_tab":
                # Task 5: no path -> clone the ACTIVE slot's own workspace
                # into a fresh tab (falling back to the process cwd only if
                # that's somehow empty too — defensive, shouldn't happen in
                # practice); a GIVEN path is resolved against the process's
                # cwd (`os.path.abspath`, the SAME anchor `workspace` itself
                # uses everywhere else in this file), not the active slot's
                # own directory — so `/new ../sibling` means the same thing
                # no matter which tab you typed it from. Always `first=False`
                # (map §6 replay landmine): only the process's very first
                # session slot ever gets the welcome banner + thread replay.
                # A fresh tab starts in the process's BASELINE `mode`, never
                # inheriting whatever the active tab is currently running in
                # — an autopilot tab must never spawn another autopilot tab
                # silently.
                ws = os.path.abspath(res.arg) if res.arg else (slot.workspace or workspace)
                new_slot = await _make_session_slot(
                    cfg, token_provider, ws, mode, resources=resources,
                    shared_client=shared_client, agent_factory=agent_factory,
                    intel_factory=intel_factory, shadow_factory=shadow_factory,
                    first=False)
                idx = slots.add(new_slot)
                ui_hooks.get("switch", slots.switch)(idx)
                new_slot.sink.note(f"tab {idx} opened — {new_slot.label}")
                return "continue"
            if res.action == "tab_switch":
                try:
                    idx = int(res.arg)
                except (TypeError, ValueError):
                    idx = -1
                if idx < 0 or idx >= len(slots.slots):
                    if _sink is not None:
                        _sink.note(f"No such tab '{res.arg}'. /tabs lists the open ones.")
                    return "continue"
                ui_hooks.get("switch", slots.switch)(idx)
                return "continue"
            if res.action == "tab_close":
                # Share the EXACT close flow the dock's Ctrl-W/✕ use
                # (`ui_hooks["close"]` == tui's `_close_flow`) when a dock is
                # actually running; the fallback (headless/no-dock) calls
                # webbee.slots.close_active directly with the SAME
                # `_cancel_slot` -- no UI to invalidate, but identical
                # Home-guard/cancel/note semantics either way.
                close_fn = ui_hooks.get("close")
                closed = close_fn() if close_fn is not None else close_active(slots, _cancel_slot)
                if not closed and _sink is not None:
                    # Guarded by SlotManager.close's own idx<=0 invariant --
                    # the dock's Home tab, or (fallback loop) the only slot
                    # there is, since it always sits at index 0 too.
                    _sink.note("Nothing to close.")
                return "continue"
            if res.action == "tabs_list":
                lines = [f"{'●' if i == slots.active_idx else '○'}{i} {s.label} {s.status_glyph()}"
                         for i, s in enumerate(slots.slots)]
                if _sink is not None:
                    _sink.note("Open tabs:\n" + "\n".join(lines))
                return "continue"
            if res.action == "queue_clear":
                # dispatch built the message (with the drop count) from the
                # ctx snapshot; here we drop the live deque — the toolbar
                # count follows on the sink's redraw below.
                slot.pending.clear()
            if res.action == "clear":
                _sink.clear()
                _sink.note(res.message)
                return "continue"
            if res.action == "mode" and res.new_mode:
                slot.mode = res.new_mode
                if slot.agent is not None:
                    slot.agent.mode = res.new_mode
            if res.message:
                _sink.note(res.message)
            return "continue"

        # A task for the agent. A drained type-ahead line minted for a
        # failed mid-turn inject still carries its steer_iid (tui.QueuedLine)
        # -- thread it so the kernel's dedup ring drops the twin if the
        # inject actually landed server-side (a plain typed line has none).
        slot = slots.active()
        slot.sink.user_echo(line)
        await _run_turn(line, steer_iid=getattr(line, "iid", ""))
        return "continue"

    async def _run_turn(line: str, surface: str = "", steer_iid: str = "") -> None:
        """ONE agent turn -- the SAME path for a typed line and an idle-steer
        pickup (liveness v2 §B), which threads the queued item's origin
        `surface` (provenance) and dedup `steer_iid` (kernel dedup ring) into
        the turn start-path. Only the echo differs at the call sites:
        user_echo for a typed line, foreign_turn for a remote one. Reads the
        ACTIVE slot at call time (map §1) -- a background/remote turn always
        targets whichever slot is active right now."""
        slot = slots.active()
        _sink = slot.sink
        _sink.begin_turn()
        kw = {"surface": surface} if surface else {}
        if steer_iid:
            kw["steer_iid"] = steer_iid
        try:
            text = await slot.agent.run(line, _sink, marathon=not once,
                                   goal=(line if not once else ""), **kw)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _sink.abort()
            _sink.note("Interrupted.")
            _sink.end_turn("")   # clear busy (poller starvation guard)
            return
        except Exception as e:  # network/auth/etc — never crash the REPL
            # W1 task 6: flag the sink so the dock's drain rule HOLDS the
            # type-ahead queue instead of burning one queued line into this
            # failing turn (getattr-guarded — minimal sinks in tests/headless
            # callers may not implement it).
            _mark = getattr(_sink, "mark_turn_failed", None)
            if _mark is not None:
                _mark()
            if type(e).__name__ in ("StreamAuthError", "NotLoggedInError"):
                _sink.note("Session expired or access revoked — run /login to sign in again.")
            else:
                _sink.note(f"Error: {type(e).__name__}: {e}")
            _sink.end_turn("")   # clear busy: a stuck 'working' also starves the idle-steer poller
            if slot.pending:
                _sink.note(f"⏸ queue held: {len(slot.pending)} queued message(s) wait "
                           "— ↑ pulls the next into the input, /queue clear drops them")
            return
        _sink.end_turn(text)

    async def _steer_submit(text: str, surface: str, steer_iid: str = "") -> None:
        """webbee.steer hands a drained remote instruction here: render it as
        the remote user's own line, then run it as a normal turn (carrying the
        item's dedup iid so the kernel can drop an at-least-once twin). Reads
        the ACTIVE slot at call time (map §6 steer policy)."""
        slots.active().sink.foreign_turn(surface, "user", text)
        await _run_turn(text, surface=surface, steer_iid=steer_iid)

    def _poller_busy() -> bool:
        """The idle-steer poller's busy gate: the ACTIVE slot's live turn
        state (LOCKOUT-PROOF via _gate_busy -- a dead turn task overrides a
        stuck busy flag) PLUS an armed local prompt (the autopilot confirm
        arms the same pinned-input future a consent uses) -- submitting a
        steer turn under an armed prompt could double-prompt the input, so
        the poller holds off until it resolves. Both hooks getattr-guarded
        (minimal test sinks)."""
        slot = slots.active()
        return _gate_busy(slot.sink, slot.turn)

    async def _stop_active_turn() -> None:
        """tui.run_session's `stop_turn` leg (Esc/Ctrl-C) -- resolves the
        ACTIVE slot's agent AT CALL TIME (W4a Task 3: the injected callable
        itself stays fixed for the whole dock lifetime; only what it reads
        is slot-aware). A Home slot has no agent -- a no-op, matching
        _busy_live's is_busy=False default there (nothing to stop)."""
        agent = slots.active().agent
        if agent is not None:
            await agent.stop()

    def _on_mode(mode: str, surface: str) -> None:
        """Remote coding-mode request (TG/panel → gateway one-shot req_mode →
        the pending-steer poll). AUTOPILOT SAFE ASYMMETRY (Valentin-chosen):
        a downgrade or lateral move (→ default/plan) applies INSTANTLY with a
        visible audited note; the upgrade → autopilot NEVER applies silently —
        a terminal-local y/n confirm must approve it (the person physically
        at the terminal is the risk bearer; a remote surface must not disarm
        the consent prompt it is about to exploit). Unknown modes and no-ops
        are dropped. Sync + non-blocking by contract (the poller calls it):
        the confirm runs as its own background task. Reads the ACTIVE slot
        at call time (map §6 steer policy)."""
        surface = surface or "remote"
        slot = slots.active()
        if mode not in _MODES or mode == slot.mode:
            return
        if mode != "autopilot":
            slot.mode = mode
            if slot.agent is not None:
                slot.agent.mode = mode
            slot.sink.note(f"mode → {mode} [{surface}]")
            return
        asyncio.ensure_future(_confirm_autopilot(surface))

    async def _confirm_autopilot(surface: str) -> None:
        """The terminal-local one-tap confirm for a remote autopilot upgrade.
        Fail-safe in every direction: no confirm affordance, a turn/prompt
        already live, anything but an explicit local yes, or the prompt
        timeout all KEEP the current mode — and both outcomes leave an
        audited note in the transcript. While the prompt is armed the steer
        poller holds off (_poller_busy gates it), so it can never collide
        with a real kernel consent (those only exist mid-turn, when the
        poller does not fetch at all). Reads the ACTIVE slot once at call
        time (map §6) -- there is no tab-switch UI yet, so this always
        targets the slot the request was raised against."""
        slot = slots.active()
        ask = getattr(slot.sink, "ask_yes_no", None)
        if ask is None or _poller_busy():
            slot.sink.note(f"autopilot request from {surface} not applied — mode stays {slot.mode}")
            return
        ok = await ask(f"{surface} asks to switch to autopilot "
                       f"(auto-approve everything) — allow? [y/n]")
        if ok:
            slot.mode = "autopilot"
            if slot.agent is not None:
                slot.agent.mode = "autopilot"
            slot.sink.note(f"mode → autopilot [{surface}] — approved at this terminal")
        else:
            slot.sink.note(f"autopilot request from {surface} declined — mode stays {slot.mode}")

    def _cancel_background() -> None:
        if steer_task is not None:
            steer_task.cancel()
        for s in slots.slots:
            for t in s.bg_tasks:
                if t is not None:
                    t.cancel()
        # Resources bundles are cached per repo ROOT (WorkspaceResources), one
        # entry per distinct workspace -- iterating the cache itself (instead
        # of per-slot) means a watcher shared by N same-repo slots is
        # cancelled exactly once, never double-cancelled.
        for bundle in resources._by_root.values():
            wt = bundle.get("watcher_task")
            if wt is not None:
                wt.cancel()

    def _spawn_steer() -> None:
        nonlocal steer_task
        from webbee import steer as _steer
        # Liveness v2 §B: idle-steer pickup. All poll/drain logic lives in
        # webbee.steer -- this is wiring only: the ACTIVE slot's live turn
        # state (+ an armed local prompt) gates polling, the ACTIVE slot's
        # agent session id (once a turn has run) is the gateway truth,
        # _steer_submit is the normal turn path, and _on_mode adopts a
        # remote mode request (autopilot safe-asymmetry). ONE poller for the
        # whole process (map §6): `workspace` stays bound to the FIRST
        # session slot's repo root -- today's `workspace` local, since W4a
        # never opens a second tab on a different repo. pending_steer is per-
        # USER gateway-side; W4b re-keys it per session instead.
        steer_task = asyncio.ensure_future(_steer.poll_idle_steer(
            cfg, token_provider, workspace=workspace, marathon=not once,
            is_busy=_poller_busy,
            live_session_id=lambda: _live_session_id(slots),
            submit=_steer_submit, on_mode=_on_mode, client=shared_client))

    if use_dock:
        ok = False
        # Route stderr to a log file for the dock's ENTIRE lifetime (boot's
        # model-download included) so no stray write can corrupt the full-screen
        # renderer. Restored the instant the dock exits (the transcript dump
        # below writes to the REAL stdout).
        _errlog = boot._open_dock_stderr_log()
        try:
            with contextlib.redirect_stderr(_errlog):
                from webbee import tui
                from webbee.sizing import get_size
                width, _height = get_size(None)   # pre-app: same fallback, one code path

                # Home slot (Task 6 fills its content -- here just an empty
                # pane) at index 0; the first session slot (cwd workspace) at
                # index 1.
                home_pane = tui.OutputPane(width=width)
                slots.add(SessionSlot(kind="home", workspace=workspace, label="Home",
                                      pane=home_pane, sink=None, agent=None))

                session_slot = await _make_session_slot(
                    cfg, token_provider, workspace, mode, resources=resources,
                    shared_client=shared_client, agent_factory=agent_factory,
                    intel_factory=intel_factory, shadow_factory=shadow_factory,
                    first=True, account=account)
                slots.add(session_slot)
                # W4a Task 2: always land in the session (Home is one Alt+0
                # away) -- Task 6 refines this to land on Home when the boot
                # replay showed nothing.
                slots.active_idx = 1
                _spawn_steer()

                async def _on_line(text: str) -> None:
                    if await _handle(text) == "exit":
                        from prompt_toolkit.application import get_app
                        get_app().exit()

                try:
                    ok = await tui.run_session(
                        slots=slots, on_line=_on_line, on_cycle=_cycle,
                        steps_nav={
                            "count": lambda: len(getattr(slots.active().agent, "steps", [])),
                            "expand": lambda i: _handle(f"/steps {i + 1}"),
                        },
                        stop_turn=_stop_active_turn,
                        queued_run=lambda n: (slots.active().sink.queued_run(n)
                                              if slots.active().sink is not None else None),
                        inject=lambda text, iid: _inject_via_gateway(
                            cfg, token_provider, slots.active().agent, slots.active().sink, text, iid,
                            client=shared_client),
                        cancel_slot=_cancel_slot, ui_hooks=ui_hooks,
                    )
                finally:
                    _cancel_background()
                    if shared_client is not None:
                        try:
                            await shared_client.aclose()
                        except Exception:
                            pass
        except Exception:
            ok = False
        finally:
            try:
                _errlog.close()
            except Exception:
                pass
        if ok:
            # the alt screen is gone — reprint the session transcript to real
            # stdout so the conversation stays in the terminal scrollback.
            # Task 3: slots.active().pane, not the fixed session_slot -- for
            # now (no tab-bar UI, no /close) this is always the same slot,
            # but the dump follows whichever tab was visible on exit.
            if slots.slots:
                sys.stdout.write(slots.active().pane.dump())
                sys.stdout.flush()
            return
        # dock unavailable/failed → fall through to the plain fallback loop

    # Fallback loop (tests / non-tty / dock unavailable) -- ONE session slot,
    # NO Home tab: every existing fallback-path test keeps its world exactly
    # as before. `session_count() == 0` also covers a dock attempt that blew
    # up before it ever built a session slot (a stray Home is discarded);
    # a dock attempt that built ITS session slot but failed later (e.g.
    # tui.run_session itself unavailable) reuses that same slot here instead
    # of re-booting (double account fetch / double replay).
    if slots.session_count() == 0:
        slots.slots.clear()
        slots.active_idx = 0
        if sink is None:
            from webbee.render import RichSink
            sink = RichSink()
        fallback_slot = await _finish_slot(
            cfg, token_provider, workspace, mode, resources=resources,
            agent_factory=agent_factory, intel_factory=intel_factory,
            shadow_factory=shadow_factory, pane=None, sink=sink,
            first=True, account=account)
        slots.add(fallback_slot)
        _spawn_steer()
    try:
        while True:
            try:
                line = read_line("❯ ")
            except (EOFError, KeyboardInterrupt):
                return
            if line is None:
                return
            if await _handle(line) == "exit":
                return
    finally:
        _cancel_background()
        if shared_client is not None:
            try:
                await shared_client.aclose()
            except Exception:
                pass
