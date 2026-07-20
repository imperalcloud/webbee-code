import asyncio
import contextlib
import os
import sys
import time

from webbee import __version__, boot, home
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


async def _inject_into_slot(cfg, token_provider, slot: SessionSlot, text: str,
                            steer_iid: str, client=None) -> bool:
    """FIX7a (W4a final review) — the dock's inject leg, slot-explicit: tui's
    `_launch_inject` captures `slot` SYNCHRONOUSLY at Enter keypress time and
    hands it straight through, so the gateway POST targets THAT slot's own
    agent/sink regardless of whatever tab becomes active before this
    coroutine's body actually runs (was: `slots.active()` resolved here, at
    call time — the same cross-tab hazard FIX1 closes for on_line). A Home
    slot (or, in principle, any slot mid-teardown) has no agent to post
    into — guarded here rather than relying on `_inject_via_gateway`'s own
    `getattr(agent, "session_id", "")` None-tolerance, so the "no agent"
    case is an explicit, obvious False rather than an incidental one."""
    if slot.agent is None:
        return False
    return await _inject_via_gateway(cfg, token_provider, slot.agent, slot.sink,
                                     text, steer_iid, client=client)


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


# FIX4 (W4a final review — Home None-sink command crashes): actions that are
# genuinely SESSION-scoped (an agent's steps, a workspace's checkpoints, this
# session's spend/queue, account operations kept off the dashboard for
# consistency) — dispatched while Home is active, they reply with ONE
# consistent "open a tab" note instead of either crashing on `_sink.note`
# (sink is None) or quietly doing session-shaped work against Home's own
# placeholder fields. `/clear`/`/tabs`/`/tab N`/`/new`/`/close`/`/help`/
# `/exit` are deliberately NOT in this set — those are tab-bar/global actions
# and must keep working on Home (map §FIX4).
_HOME_GATED_ACTIONS = frozenset({
    "steps", "step_detail", "checkpoints", "rollback", "notify", "mode",
    "cost", "queue", "queue_clear", "login", "logout", "sessions",
    "sessions_revoke", "logout_others",
})

_HOME_GATE_NOTE = "open a session tab first — Ctrl+T or type a task"


def _say(slot: SessionSlot, msg: str) -> None:
    """Reply into `slot`'s own surface regardless of whether it has a live
    sink -- a REAL session slot's `sink.note(msg)`, unchanged; a sink-less
    slot (Home) prints the SAME message straight into its own pane console,
    styled like a note (the SAME dim/bee accent + gutter `render._pad`/
    `_clean` a sink's own `.note` uses), so a command dispatched while Home
    is active still gets an honest answer instead of an unguarded
    AttributeError crash on `None.note(...)`. Never raises: a pane with no
    console (shouldn't happen -- every slot gets one) is simply a no-op."""
    if slot.sink is not None:
        slot.sink.note(msg)
        return
    console = getattr(slot.pane, "console", None)
    if console is None:
        return
    from rich.text import Text

    from webbee.render import _BEE, _clean, _pad
    console.print(_pad(Text(_clean(msg), style=_BEE)))
    notify = getattr(slot.pane, "notify", None)
    if notify is not None:
        notify()


def _live_session_id(slots: SlotManager) -> str:
    """The idle-steer poller's live-session seam (map §1): the ACTIVE slot's
    agent session id, or "" with no crash when the active slot has no agent
    at all (Home)."""
    agent = slots.active().agent
    return getattr(agent, "session_id", "") if agent is not None else ""


def _steer_target(slots: SlotManager) -> SessionSlot | None:
    """Steer-poller targeting (Task 7, map contract item 1): a remote
    instruction must land in a SESSION slot — Home has no sink/agent to run
    a turn against, so routing a steer submit there would crash. The ACTIVE
    slot already IS a session -> itself, exactly today's behavior. Home
    active -> the FIRST session slot (lowest index) -- "remote steer must
    never dead-end" (brief). No session slot exists at all (every tab closed
    down to bare Home) -> None; both `_poller_busy` and `_steer_submit` treat
    that as "nothing to submit into" rather than reaching for a None sink.
    Module-level so a test drives it directly, same as `_live_session_id`."""
    active = slots.active()
    if active.kind == "session":
        return active
    for slot in slots.slots:
        if slot.kind == "session":
            return slot
    return None


def _exit_dump(slots: SlotManager) -> str:
    """The dock's post-exit scrollback dump (Task 7, map contract item 2):
    EVERY session slot's pane, in index order — the transcript keeps
    everything, not just whichever tab happened to be visible when the dock
    quit. Home is skipped outright (a live dashboard, not a conversation --
    dumping it would just print stale UI chrome to real stdout). A
    `── tab N: {label} ──` separator (N = the slot's own SlotManager index,
    the same number `/tab N` and the tab bar use) lands BETWEEN panes, never
    before the first or after the last -- one session slot degrades to a
    bare `pane.dump()` with no separator at all, byte-identical to the
    single-tab world this replaces. Pure (no I/O) so a test drives it
    directly instead of needing a real dock + real stdout."""
    sessions = [(i, s) for i, s in enumerate(slots.slots) if s.kind == "session"]
    parts = []
    for n, (i, s) in enumerate(sessions):
        if n > 0:
            parts.append(f"── tab {i}: {s.label} ──\n")
        parts.append(s.pane.dump())
    return "".join(parts)


def _cancel_all_background(steer_task, slots: SlotManager, resources: WorkspaceResources) -> None:
    """Exit-time teardown, final shape (Task 7, map contract item 4): the ONE
    process-wide steer poller + EVERY slot's own bg_tasks (today only Home's
    fill_home, map §1 -- any later per-slot background piece sweeps here too,
    for free) + every distinct-repo-root WorkspaceResources bundle's
    watcher_task -- via the PUBLIC `resources.bundles()` accessor (never the
    private `_by_root` dict this file used to poke directly). `.done()`-
    guarded throughout (same discipline as `_cancel_slot`): an already-
    finished task is never double-cancelled. Module-level so a test drives it
    directly with fake tasks, instead of needing a live run_repl exit."""
    if steer_task is not None:
        steer_task.cancel()
    for s in slots.slots:
        for t in s.bg_tasks:
            if t is not None and not t.done():
                t.cancel()
    for bundle in resources.bundles():
        wt = bundle.get("watcher_task")
        if wt is not None and not wt.done():
            wt.cancel()


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
    against the same repo later re-attaches to a run that never stopped.

    FIX2 (W4a final review — ghost drain on close): `turn["stopped"] = True`
    is set BEFORE the cancel, mirroring `_escape_action`/`_interrupt_action`
    (the SAME "user is taking control" flag a Esc/Ctrl-C stop sets). Without
    it, tui's `_run_turn` catches the CancelledError inside `agent.run` and
    returns normally -- `done=True`, `stopped` absent -- so its finally block
    happily DRAINS whatever was still queued into a brand-new turn on a slot
    that no longer exists anywhere in the SlotManager: a ghost turn, invisible
    to any tab, spending against a closed session. Setting the flag here
    makes closing indistinguishable from any other user stop -- the queue is
    preserved on the (now-detached) slot and simply goes away with it, never
    drained."""
    slot.turn["stopped"] = True
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
                       account) -> "tuple[SessionSlot, int]":
    """Shared tail of slot construction -- the dock's `_make_session_slot` AND
    the headless fallback loop both fall into this once they have their own
    pane (or None, fallback has no dock) + sink: resolve/boot the per-
    WORKSPACE resources bundle, build the agent, wire the sink's local queue
    to THIS slot's own deque, and -- gated by `first` (map §6 replay
    landmine) -- show the welcome banner and replay the durable thread. ONLY
    the very first session slot the process ever creates does either; every
    later tab (first=False) skips both. Returns `(slot, replayed)` (FIX7e)
    -- `replayed` is `boot.replay_thread`'s own display-message count (0 when
    `first=False`, or the replay itself skipped/failed/found nothing)."""
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
    replayed = 0
    if first:
        if account is not None:
            sink.welcome(account, workspace, "terminal")
        # Boot replay of the durable per-user thread (Task 9) — best-effort,
        # never a boot blocker (webbee.boot.replay_thread swallows
        # everything). Landmine (map §6): replay is keyed by a per-USER
        # placeholder, not per-workspace -- a second slot replaying it would
        # show the wrong (or duplicate) history, so only the first session
        # slot the process ever creates gets it.
        replayed = await boot.replay_thread(cfg, token_provider, sink)
    return slot, replayed


async def _make_session_slot(cfg, token_provider, workspace, mode, *, resources: WorkspaceResources,
                             shared_client, agent_factory, intel_factory, shadow_factory,
                             first: bool, account=None, _with_replayed: bool = False):
    """Builds ONE dock tab's atomic {agent, sink, pane} triple (map §6 —
    created together, a sink must never point at another slot's pane/
    console). `shared_client` isn't consumed here yet — reserved for Task 3's
    per-slot inject/steer wiring; accepted here for interface parity with the
    seams this factory feeds. Returns the bare `SessionSlot` (every existing
    caller — `/new`, `_home_input`, every direct test) unless
    `_with_replayed=True` (FIX7e — the dock boot's OWN first-slot call,
    the only caller that needs `boot.replay_thread`'s count to decide
    `slots.active_idx`), which returns `(slot, replayed)` instead."""
    from webbee import tui
    from webbee.render import RichSink
    from webbee.sizing import get_size

    width, _height = get_size(None)   # pre-app: same fallback tui.run_session's own sizing uses
    pane = tui.OutputPane(width=width)
    sink = RichSink(console=pane.console, on_output=pane.notify)
    slot, replayed = await _finish_slot(cfg, token_provider, workspace, mode, resources=resources,
                                        agent_factory=agent_factory, intel_factory=intel_factory,
                                        shadow_factory=shadow_factory, pane=pane, sink=sink,
                                        first=first, account=account)
    return (slot, replayed) if _with_replayed else slot


def _home_target_workspace(slots: SlotManager, cwd: str) -> str:
    """Home's own workspace pick (Task 6 `home_input`): the most recently
    OPENED session tab's own directory — continue wherever you're already
    working instead of a bare process cwd — falling back to `cwd` only when
    no session tab exists at all yet. Deliberately NOT `slots.active()`:
    this is only ever called while Home itself is the active slot (that's
    the only way `home_input` fires at all), so `active()` would just be
    Home's own (uninteresting) workspace field."""
    for slot in reversed(slots.slots):
        if slot.kind == "session":
            return slot.workspace
    return cwd


async def _home_input(text: str, *, slots: SlotManager, cfg, token_provider, mode: str,
                      resources: WorkspaceResources, shared_client, agent_factory,
                      intel_factory, shadow_factory, workspace: str,
                      ui_hooks: dict, run_turn) -> None:
    """Home's Enter path (Task 6, wired into `tui.run_session` as
    `home_input=`): typing a task on Home opens a session tab in one motion
    — the SAME `_make_session_slot`/switch path the `/new` command uses
    (always `first=False`, always the process's BASELINE `mode`, per the
    replay landmine and the autopilot-never-inherited rule both documented
    on the `new_tab` action above) — then runs the typed text as that NEW
    slot's own first turn, explicitly against `new_slot` (FIX1: `run_turn` --
    repl's `_run_turn` -- is slot-explicit now, never resolves
    `slots.active()` internally). FIX3: the turn is started through
    `ui_hooks["start_turn_in"]` (tui's own `_start_turn_in`, the SAME seam a
    normal Enter-idle submit uses) when a dock is running, so
    `new_slot.turn["task"]` is actually populated -- without this the
    dock-spawned first turn ran invisibly: no busy glyph, no Esc/Ctrl-C
    cancel, since nothing ever recorded it as "this slot's live turn". No
    dock (`ui_hooks` has no `start_turn_in` -- fallback loop / direct tests)
    falls back to a plain blocking `await run_turn(new_slot, text)`, same as
    before this fix, since there is no turn-visibility contract to satisfy
    outside a dock. No "tab opened" note (unlike `/new`): starting to type IS
    the deliberate action here: announcing it too would just repeat the ❯
    echo that follows immediately after. Module-level + fully parameterized
    (not a closure) so a test can drive it directly, same testing philosophy
    as `_finish_slot`/`_make_session_slot`."""
    ws = _home_target_workspace(slots, workspace)
    new_slot = await _make_session_slot(
        cfg, token_provider, ws, mode, resources=resources,
        shared_client=shared_client, agent_factory=agent_factory,
        intel_factory=intel_factory, shadow_factory=shadow_factory, first=False)
    idx = slots.add(new_slot)
    ui_hooks.get("switch", slots.switch)(idx)
    new_slot.sink.user_echo(text)
    start_turn_in = ui_hooks.get("start_turn_in")
    if start_turn_in is not None:
        start_turn_in(new_slot, text)
    else:
        await run_turn(new_slot, text)


def _schedule_home_refill(slots: SlotManager, idx: int, fill_kwargs: dict, *,
                          now: float | None = None, fill_home=None) -> bool:
    """The switch-to-Home refill hook (Task 6, wired as `tui.run_session`'s
    `on_switch`): fires on EVERY tab switch (click, Ctrl-T, Alt+N, or a
    `/tab`/`/new` command via `ui_hooks["switch"]` — they all resolve to the
    same `tui._switch_to`), but only actually schedules work when `idx` is
    Home (0) AND its content is stale (`home.is_stale`) — a fresh Home
    (just booted, or refilled within the last `ttl`) is a no-op on every
    other switch. `fill_home` itself is the REAL concurrency guard against a
    genuine double-fill (its own `_filling` flag, checked first thing,
    before any await) — this wrapper's own job is only to avoid spawning a
    throwaway task when nothing is stale at all. `fill_home=`/`now=` are DI
    seams for tests; production always resolves `webbee.home.fill_home` +
    `time.monotonic()`. Returns True iff a bg task was appended (test-
    visible signal), never raises."""
    if idx != 0 or not slots.slots:
        return False
    if fill_home is None:
        fill_home = home.fill_home
    slot = slots.slots[0]
    if not home.is_stale(slot, now if now is not None else time.monotonic()):
        return False
    slot.bg_tasks.append(asyncio.ensure_future(fill_home(slot, **fill_kwargs)))
    return True


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

    # Task 6: the exact kwargs `home.fill_home` needs, minted once and
    # reused for both the boot fill and every later stale-switch refill
    # (`_schedule_home_refill`) — `account_fetcher` is threaded through
    # verbatim so Home's OWN identity-tile fetch is independent of the
    # process-wide `account` above (best-effort, may re-fetch — Home is
    # filled in the background regardless of whether it ever becomes
    # visible, so it never blocks boot on a second network round-trip).
    home_fill_kwargs = dict(cfg=cfg, token_provider=token_provider, slots=slots,
                            account_fetcher=account_fetcher, sessions_client=sessions_client,
                            resources=resources, version=__version__)

    def _resources_for(ws: str) -> dict:
        return resources.get(ws) or {}

    def _cycle() -> None:
        slot = slots.active()
        slot.mode = next_mode(slot.mode)
        if slot.agent is not None:
            slot.agent.mode = slot.mode

    async def _handle(line: str, slot: SessionSlot) -> str:
        """Process one input line AGAINST an EXPLICIT slot (FIX1, W4a final
        review) -- every caller pins the slot the line actually belongs to
        (the dock's `_on_line(text, slot)`, the fallback loop's own
        `slots.active()`, `steps_nav["expand"]`) instead of this function
        re-resolving `slots.active()` internally, which used to let a drain
        (or the turn itself) land in whatever tab happened to be VISIBLE by
        the time its background task's body actually ran -- cross-tab
        execution, never the tab the line was typed into. Returns 'exit' or
        'continue'."""
        if not line.strip():
            return "continue"
        res = dispatch(line, _slot_ctx(slot, logged_in=state["logged_in"]))
        if res.handled:
            _sink = slot.sink
            if res.exit:
                return "exit"
            if _sink is None and res.action in _HOME_GATED_ACTIONS:
                # FIX4: Home has no sink/agent/workspace-scoped session to
                # act against -- a consistent redirect beats either crashing
                # on `_sink.note` (sink is None) or quietly running
                # session-shaped logic against Home's own placeholder
                # fields. Global/tab actions (help/clear/tabs/tab/new/close/
                # exit) are NOT in `_HOME_GATED_ACTIONS` and fall through
                # to their own handlers below, unaffected.
                _say(slot, _HOME_GATE_NOTE)
                return "continue"
            if res.action == "login":
                # Device-code flow (RFC 8628) — rendering + polling in webbee.account.
                email = await login_device_flow(cfg, auth, _sink)
                state["logged_in"] = True
                _say(slot, f"Signed in as {email}.")
                return "continue"
            if res.action == "logout":
                await auth.logout(cfg)
                state["logged_in"] = False
                _say(slot, "Signed out, local credentials removed.")
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
                    _say(slot, "Usage: /sessions revoke <#> — run /sessions first to see the list.")
                    return "continue"
                s = rows[idx]
                if s.get("current"):
                    _say(slot, "That's this terminal — use /logout to sign out here.")
                    return "continue"
                ok = await sessions_client.revoke_session(cfg, token_provider, s["session_id"])
                _say(slot, f"Revoked {s.get('label') or s.get('surface')}." if ok else "Failed to revoke session.")
                return "continue"
            if res.action == "logout_others":
                n = await sessions_client.revoke_others(cfg, token_provider)
                _say(slot, f"Signed out {n} other session(s)." if n >= 0 else "Failed to sign out other sessions.")
                return "continue"
            if res.action == "steps":
                from webbee.details import format_steps
                _say(slot, format_steps(getattr(slot.agent, "steps", [])))
                return "continue"
            if res.action == "step_detail":
                from webbee.details import build_step_ref, fetch_step_detail, format_steps
                _steps = getattr(slot.agent, "steps", [])
                try:
                    _idx = int(res.arg) - 1
                    _step = _steps[_idx]
                except (ValueError, IndexError):
                    _say(slot, f"No such step. {format_steps(_steps)}")
                    return "continue"
                if not _step.get("step_id") or not getattr(slot.agent, "session_id", ""):
                    _say(slot, "No detail ref for this step.")
                    return "continue"
                _detail = await fetch_step_detail(
                    cfg, token_provider, build_step_ref(slot.agent.session_id, _step["step_id"]))
                if _detail:
                    _sink.step_detail(_detail)
                else:
                    _say(slot, "Detail unavailable (expired or not recorded).")
                return "continue"
            if res.action == "checkpoints":
                shadow = _resources_for(slot.workspace).get("shadow")
                if shadow is None:
                    _say(slot, "Reversibility is off (git unavailable).")
                else:
                    _say(slot, await asyncio.to_thread(shadow.describe))
                return "continue"
            if res.action == "rollback":
                shadow = _resources_for(slot.workspace).get("shadow")
                if shadow is None:
                    _say(slot, "Reversibility is off (git unavailable).")
                elif not res.arg:
                    _say(slot, "Usage: /rollback <id|cp-N|N>  (see /checkpoints)")
                else:
                    _r = await asyncio.to_thread(shadow.rollback, res.arg)
                    _say(slot, str(_r.get("content", "")))
                return "continue"
            if res.action == "notify":
                from webbee import remote as _remote
                sid = getattr(slot.agent, "session_id", "")
                if not sid:
                    _say(slot, "Start a coding turn first, then /notify to route it.")
                    return "continue"
                try:
                    if res.arg in ("tg", "panel", "both", "off"):
                        st = await _remote.set_remote(cfg, token_provider, sid, res.arg)
                    elif res.arg:
                        _say(slot, "Usage: /notify [tg|panel|both|off]")
                        return "continue"
                    else:
                        st = await _remote.get_remote(cfg, token_provider, sid)
                    _say(slot, _remote.describe(st))
                except Exception as e:
                    _say(slot, f"Remote control unavailable: {type(e).__name__}")
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
                    _say(slot, f"No such tab '{res.arg}'. /tabs lists the open ones.")
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
                if not closed:
                    # Guarded by SlotManager.close's own idx<=0 invariant --
                    # the dock's Home tab, or (fallback loop) the only slot
                    # there is, since it always sits at index 0 too.
                    _say(slot, "Nothing to close.")
                return "continue"
            if res.action == "tabs_list":
                # FIX4: works fully on Home (a global/tab action, never
                # gated) -- unconditional _say instead of the old
                # `if _sink is not None:` guard, which silently swallowed
                # the listing instead of showing it.
                lines = [f"{'●' if i == slots.active_idx else '○'}{i} {s.label} {s.status_glyph()}"
                         for i, s in enumerate(slots.slots)]
                _say(slot, "Open tabs:\n" + "\n".join(lines))
                return "continue"
            if res.action == "queue_clear":
                # dispatch built the message (with the drop count) from the
                # ctx snapshot; here we drop the live deque — the toolbar
                # count follows on the sink's redraw below. (Home never
                # reaches here -- "queue_clear" is in _HOME_GATED_ACTIONS.)
                slot.pending.clear()
            if res.action == "clear":
                # FIX4: works fully on Home too -- clears Home's OWN pane
                # (there are no session counters to reset there, so only the
                # sink branch resets them, exactly like before this fix).
                if _sink is not None:
                    _sink.clear()
                else:
                    slot.pane.console.clear()
                _say(slot, res.message)
                return "continue"
            if res.action == "mode" and res.new_mode:
                # Home never reaches here -- "mode" is in _HOME_GATED_ACTIONS,
                # so Home's own slot.mode is never mutated by a remote /mode.
                slot.mode = res.new_mode
                if slot.agent is not None:
                    slot.agent.mode = res.new_mode
            if res.message:
                _say(slot, res.message)
            return "continue"

        # A task for the agent. A drained type-ahead line minted for a
        # failed mid-turn inject still carries its steer_iid (tui.QueuedLine)
        # -- thread it so the kernel's dedup ring drops the twin if the
        # inject actually landed server-side (a plain typed line has none).
        slot.sink.user_echo(line)
        await _run_turn(slot, line, steer_iid=getattr(line, "iid", ""))
        return "continue"

    async def _run_turn_on(slot: SessionSlot, line: str, surface: str = "", steer_iid: str = "") -> None:
        """ONE agent turn against an EXPLICIT slot (Task 7 split -- was:
        always `slots.active()` internally, which broke a steer submit that
        deliberately targets a DIFFERENT slot than whatever's on screen, see
        `_steer_target`). `_run_turn` below is the thin active()-resolving
        wrapper every typed-line call site keeps using unchanged."""
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

    async def _run_turn(slot: SessionSlot, line: str, surface: str = "", steer_iid: str = "") -> None:
        """ONE agent turn -- the typed-line path (FIX1: slot-explicit
        end-to-end, was: resolved `slots.active()` internally, map §1's
        original design). The caller (`_handle`, `_home_input`) always hands
        the slot the line actually belongs to, pinned at the moment the turn
        started -- never whatever tab happens to be active when this
        coroutine's body actually runs. `_steer_submit` below bypasses this
        entirely and calls `_run_turn_on` directly against `_steer_target`'s
        pick, which is NOT always the active slot (Home active -> the first
        session slot)."""
        await _run_turn_on(slot, line, surface=surface, steer_iid=steer_iid)

    async def _steer_submit(text: str, surface: str, steer_iid: str = "") -> None:
        """webbee.steer hands a drained remote instruction here: render it as
        the remote user's own line, then run it as a normal turn (carrying the
        item's dedup iid so the kernel can drop an at-least-once twin).
        Targets `_steer_target(slots)` (Task 7, map contract item 1) -- a
        SESSION slot, never Home: the active slot when it's already a
        session, otherwise the first session slot, so a remote instruction
        never dead-ends just because Home happens to be on screen. No session
        slot exists at all (every tab closed) -> fail-soft, sink-less
        no-op -- the poller's own contract is silence on nothing-to-do, not a
        crash or a note nobody would see anyway."""
        slot = _steer_target(slots)
        if slot is None:
            return
        slot.sink.foreign_turn(surface, "user", text)
        await _run_turn_on(slot, text, surface=surface, steer_iid=steer_iid)

    def _poller_busy() -> bool:
        """The idle-steer poller's busy gate: the TARGET slot's (Task 7 --
        was: blindly the active slot) live turn state (LOCKOUT-PROOF via
        _gate_busy -- a dead turn task overrides a stuck busy flag) PLUS an
        armed local prompt (the autopilot confirm arms the same pinned-input
        future a consent uses) -- submitting a steer turn under an armed
        prompt could double-prompt the input, so the poller holds off until
        it resolves. No session slot to target at all -> treat as busy (hold
        the poller) rather than fetch into a void `_steer_submit` would just
        drop. Both hooks getattr-guarded (minimal test sinks)."""
        slot = _steer_target(slots)
        if slot is None:
            return True
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
        # Task 7: the actual walk moved to the module-level, directly-tested
        # `_cancel_all_background` (steer + every slot's bg_tasks, .done()-
        # guarded + every WorkspaceResources bundle's watcher_task via the
        # PUBLIC `resources.bundles()`, never `_by_root` poked directly).
        _cancel_all_background(steer_task, slots, resources)

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

                # Home slot at index 0; the first session slot (cwd
                # workspace) at index 1.
                home_pane = tui.OutputPane(width=width)
                home_slot = SessionSlot(kind="home", workspace=workspace, label="Home",
                                        pane=home_pane, sink=None, agent=None)
                slots.add(home_slot)

                session_slot, replayed = await _make_session_slot(
                    cfg, token_provider, workspace, mode, resources=resources,
                    shared_client=shared_client, agent_factory=agent_factory,
                    intel_factory=intel_factory, shadow_factory=shadow_factory,
                    first=True, account=account, _with_replayed=True)
                slots.add(session_slot)
                # FIX7e (W4a final review — land-on-Home): the session tab
                # is only the more useful FIRST screen when the boot replay
                # actually showed something -- a fresh/empty thread lands on
                # Home instead (the new-tab dashboard), one Alt+1 away.
                slots.active_idx = 1 if replayed else 0
                _spawn_steer()
                # Task 6: fill Home in the background from the moment it
                # exists -- never blocks reaching the session tab above, and
                # is very likely already done (or well under way) by the
                # time anyone actually switches to it.
                home_slot.bg_tasks.append(asyncio.ensure_future(home.fill_home(home_slot, **home_fill_kwargs)))

                async def _on_line(text: str, slot: SessionSlot) -> None:
                    if await _handle(text, slot) == "exit":
                        from prompt_toolkit.application import get_app
                        get_app().exit()

                try:
                    ok = await tui.run_session(
                        slots=slots, on_line=_on_line, on_cycle=_cycle,
                        steps_nav={
                            "count": lambda: len(getattr(slots.active().agent, "steps", [])),
                            "expand": lambda i, slot: _handle(f"/steps {i + 1}", slot),
                        },
                        stop_turn=_stop_active_turn,
                        queued_run=lambda n: (slots.active().sink.queued_run(n)
                                              if slots.active().sink is not None else None),
                        inject=lambda text, iid, slot: _inject_into_slot(
                            cfg, token_provider, slot, text, iid, client=shared_client),
                        cancel_slot=_cancel_slot, ui_hooks=ui_hooks,
                        home_input=lambda text: _home_input(
                            text, slots=slots, cfg=cfg, token_provider=token_provider, mode=mode,
                            resources=resources, shared_client=shared_client, agent_factory=agent_factory,
                            intel_factory=intel_factory, shadow_factory=shadow_factory,
                            workspace=workspace, ui_hooks=ui_hooks, run_turn=_run_turn),
                        on_switch=lambda idx: _schedule_home_refill(slots, idx, home_fill_kwargs),
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
            # the alt screen is gone — reprint EVERY session tab's own
            # scrollback to real stdout (Task 7: was just the one slot that
            # happened to be visible on exit -- `_exit_dump` walks all of
            # them, Home skipped, a separator between panes when there's
            # more than one).
            dump = _exit_dump(slots)
            if dump:
                sys.stdout.write(dump)
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
        fallback_slot, _replayed = await _finish_slot(
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
            if await _handle(line, slots.active()) == "exit":
                return
    finally:
        _cancel_background()
        if shared_client is not None:
            try:
                await shared_client.aclose()
            except Exception:
                pass
