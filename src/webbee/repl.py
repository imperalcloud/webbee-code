import asyncio
import contextlib
import os
import sys
from collections import deque

from webbee import __version__
from webbee.account import login_device_flow
from webbee.boot import _git_branch, _open_dock_stderr_log, replay_thread, start_intel, start_shadow
from webbee.commands import CommandContext, dispatch
from webbee.session import AgentSession
from webbee.tui import next_mode


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
        sink.note(f"Error: {type(e).__name__}: {e}")
        sink.end_turn("")   # clear busy: a stuck 'working' also starves the idle-steer poller
        return ""
    sink.end_turn(text)
    return text


async def run_repl(cfg, mode: str = "default", *, once: bool = False, sink=None, read_line=input,
                   agent_factory=None, auth=None, account_fetcher=None,
                   sessions_client=None, intel_factory=None, shadow_factory=None) -> None:
    """Interactive coding REPL. Production (a real tty, no injected sink) runs
    the persistent prompt_toolkit dock (`tui.run_session`): the bordered input
    box is pinned at the bottom, turn output scrolls above it (patch_stdout →
    native scrollback), and turns run as background tasks. Tests / non-tty use
    the injected sync `read_line` fallback loop. Both share `_handle`."""
    if auth is None:
        from imperal_mcp import auth as _auth
        auth = _auth
    if agent_factory is None:
        agent_factory = lambda c, tp, ws, m: AgentSession(c, tp, ws, m, intel=intel, shadow=shadow)  # noqa: E731
    if account_fetcher is None:
        from webbee.account import fetch_account as account_fetcher
    if sessions_client is None:
        from webbee import sessions as sessions_client

    workspace = os.getcwd()

    from webbee.tokens import make_token_provider
    token_provider = make_token_provider(cfg, auth)

    # Prod dock path = the default reader + a real tty + no injected sink; tests
    # inject sink/read_line and take the plain fallback loop.
    use_dock = sink is None and read_line is input and sys.stdin.isatty()
    state = {"mode": mode, "logged_in": False}
    # Type-ahead queue: OWNED here and shared with tui.run_session, so /queue
    # and /queue clear (dispatched through CommandContext.queued, same
    # mechanism /status uses for session state) see the live deque.
    pending_queue: deque = deque()
    _sink = None         # assigned by _boot
    agent = None         # assigned by _boot
    intel = None         # assigned by _boot -- IntelService, or None (off/base-install/boot failure)
    shadow = None        # assigned by _boot -- ShadowGit, or None (git unavailable / boot failure)
    watcher_task = None  # assigned by _boot -- background watchfiles task, cancelled on exit
    steer_task = None    # assigned by _boot -- idle-steer poller (webbee.steer), cancelled on exit

    def _cycle() -> None:
        state["mode"] = next_mode(state["mode"])
        agent.mode = state["mode"]

    def _ctx() -> CommandContext:
        return CommandContext(mode=state["mode"], workspace=workspace, version=__version__,
                              surface="terminal", logged_in=state["logged_in"],
                              session_tokens=getattr(_sink, "session_tokens", 0),
                              session_credits=getattr(_sink, "session_credits", 0),
                              git_branch=state.get("git_branch", "-"),
                              queued=tuple(pending_queue))

    async def _handle(line: str) -> str:
        """Process one input line. Returns 'exit' or 'continue'."""
        if not line.strip():
            return "continue"
        res = dispatch(line, _ctx())
        if res.handled:
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
                _sink.note(format_steps(getattr(agent, "steps", [])))
                return "continue"
            if res.action == "step_detail":
                from webbee.details import build_step_ref, fetch_step_detail, format_steps
                _steps = getattr(agent, "steps", [])
                try:
                    _idx = int(res.arg) - 1
                    _step = _steps[_idx]
                except (ValueError, IndexError):
                    _sink.note(f"No such step. {format_steps(_steps)}")
                    return "continue"
                if not _step.get("step_id") or not getattr(agent, "session_id", ""):
                    _sink.note("No detail ref for this step.")
                    return "continue"
                _detail = await fetch_step_detail(
                    cfg, token_provider, build_step_ref(agent.session_id, _step["step_id"]))
                if _detail:
                    _sink.step_detail(_detail)
                else:
                    _sink.note("Detail unavailable (expired or not recorded).")
                return "continue"
            if res.action == "checkpoints":
                if shadow is None:
                    _sink.note("Reversibility is off (git unavailable).")
                else:
                    _sink.note(await asyncio.to_thread(shadow.describe))
                return "continue"
            if res.action == "rollback":
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
                sid = getattr(agent, "session_id", "")
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
            if res.action == "queue_clear":
                # dispatch built the message (with the drop count) from the
                # ctx snapshot; here we drop the live deque — the toolbar
                # count follows on the sink's redraw below.
                pending_queue.clear()
            if res.action == "clear":
                _sink.clear()
                _sink.note(res.message)
                return "continue"
            if res.action == "mode" and res.new_mode:
                state["mode"] = res.new_mode
                agent.mode = res.new_mode
            if res.message:
                _sink.note(res.message)
            return "continue"

        # A task for the agent.
        _sink.user_echo(line)
        await _run_turn(line)
        return "continue"

    async def _run_turn(line: str, surface: str = "", steer_iid: str = "") -> None:
        """ONE agent turn -- the SAME path for a typed line and an idle-steer
        pickup (liveness v2 §B), which threads the queued item's origin
        `surface` (provenance) and dedup `steer_iid` (kernel dedup ring) into
        the turn start-path. Only the echo differs at the call sites:
        user_echo for a typed line, foreign_turn for a remote one."""
        _sink.begin_turn()
        kw = {"surface": surface} if surface else {}
        if steer_iid:
            kw["steer_iid"] = steer_iid
        try:
            text = await agent.run(line, _sink, marathon=not once,
                                   goal=(line if not once else ""), **kw)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _sink.abort()
            _sink.note("Interrupted.")
            _sink.end_turn("")   # clear busy (poller starvation guard)
            return
        except Exception as e:  # network/auth/etc — never crash the REPL
            _sink.note(f"Error: {type(e).__name__}: {e}")
            _sink.end_turn("")   # clear busy: a stuck 'working' also starves the idle-steer poller
            return
        _sink.end_turn(text)

    async def _steer_submit(text: str, surface: str, steer_iid: str = "") -> None:
        """webbee.steer hands a drained remote instruction here: render it as
        the remote user's own line, then run it as a normal turn (carrying the
        item's dedup iid so the kernel can drop an at-least-once twin)."""
        _sink.foreign_turn(surface, "user", text)
        await _run_turn(text, surface=surface, steer_iid=steer_iid)

    def _cancel_background() -> None:
        for _t in (watcher_task, steer_task):
            if _t is not None:
                _t.cancel()

    async def _boot(s) -> None:
        nonlocal _sink, agent, intel, watcher_task, shadow, steer_task
        _sink = s
        # Cache git branch OFF the event loop (subprocess.run blocks it). Only
        # /status reads it; recomputing it per input line froze the dock.
        state["git_branch"] = await asyncio.to_thread(_git_branch, workspace)
        account = await account_fetcher(cfg, token_provider)
        state["logged_in"] = account.signed_in
        _sink.welcome(account, workspace, "terminal")
        # Boot replay of the durable per-user thread (Task 9) — best-effort,
        # never a boot blocker (webbee.boot.replay_thread swallows everything).
        await replay_thread(cfg, token_provider, _sink)
        if cfg.intel_enabled:
            # Guarded off-loop build + watcher; any failure degrades to
            # intel=None, never crashes the boot (webbee.boot.start_intel).
            intel, watcher_task = await start_intel(cfg, workspace, intel_factory)
        # Whole-mind P4: the reversibility shadow (never the user's VCS);
        # guarded -- boot must not fail over the time machine.
        shadow = await start_shadow(cfg, workspace, shadow_factory)
        agent = agent_factory(cfg, token_provider, workspace, state["mode"])
        # Liveness v2 §B: idle-steer pickup. All poll/drain logic lives in
        # webbee.steer -- this is wiring only: the sink's live turn state
        # gates polling, the agent's session id (once a turn has run) is the
        # gateway truth, and _steer_submit is the normal turn path.
        from webbee import steer as _steer
        steer_task = asyncio.ensure_future(_steer.poll_idle_steer(
            cfg, token_provider, workspace=workspace, marathon=not once,
            is_busy=lambda: bool(getattr(_sink, "is_busy", None) and _sink.is_busy()),
            live_session_id=lambda: getattr(agent, "session_id", ""),
            submit=_steer_submit))

    if use_dock:
        ok = False
        pane = None
        # Route stderr to a log file for the dock's ENTIRE lifetime (boot's
        # model-download included) so no stray write can corrupt the full-screen
        # renderer. Restored the instant the dock exits (the transcript dump
        # below writes to the REAL stdout).
        _errlog = _open_dock_stderr_log()
        try:
            with contextlib.redirect_stderr(_errlog):
                import shutil

                from webbee import tui
                from webbee.render import RichSink
                width = shutil.get_terminal_size((100, 24)).columns
                pane = tui.OutputPane(width=width)
                await _boot(RichSink(console=pane.console, on_output=pane.notify))

                async def _on_line(text: str) -> None:
                    if await _handle(text) == "exit":
                        from prompt_toolkit.application import get_app
                        get_app().exit()

                try:
                    ok = await tui.run_session(
                        pane=pane, on_line=_on_line, mode_getter=lambda: state["mode"],
                        on_cycle=_cycle, status=_sink.status, is_busy=_sink.is_busy,
                        consent_pending=_sink.consent_pending, resolve_consent=_sink.resolve_consent,
                        steps_nav={
                            "count": lambda: len(getattr(agent, "steps", [])),
                            "expand": lambda i: _handle(f"/steps {i + 1}"),
                        },
                        stop_turn=lambda: agent.stop(),
                        pending=pending_queue, queued_run=_sink.queued_run,
                    )
                finally:
                    _cancel_background()
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
            if pane is not None:
                sys.stdout.write(pane.dump())
                sys.stdout.flush()
            return
        # dock unavailable → fall through to the plain fallback loop

    # Fallback loop (tests / non-tty / dock unavailable).
    if _sink is None:
        if sink is None:
            from webbee.render import RichSink
            sink = RichSink()
        await _boot(sink)
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
