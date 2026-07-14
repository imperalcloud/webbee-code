import asyncio
import contextlib
import os
import subprocess
import sys

from webbee import __version__
from webbee.commands import CommandContext, dispatch
from webbee.session import AgentSession
from webbee.tui import next_mode


def _git_branch(workspace: str) -> str:
    try:
        p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace,
                           capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else "-"
    except (OSError, subprocess.SubprocessError):
        return "-"


def _open_dock_stderr_log():
    """A file to swallow stderr for the full-screen dock's lifetime. The dock is
    a prompt_toolkit full-screen Application that OWNS the terminal (it diffs the
    screen); ANY stray write to stderr while it runs — a dependency's tqdm
    download bar, a library warning, a background watcher-task traceback —
    desyncs that diff and shows up as overlapping/duplicated text. Routing
    stderr to ~/.cache/webbee/tui-stderr.log keeps the dock pixel-clean while
    still preserving errors for debugging. Falls back to os.devnull if the cache
    dir is unwritable; NEVER raises."""
    try:
        d = os.path.expanduser("~/.cache/webbee")
        os.makedirs(d, exist_ok=True)
        return open(os.path.join(d, "tui-stderr.log"), "a", buffering=1, encoding="utf-8")
    except Exception:
        try:
            return open(os.devnull, "w")
        except Exception:
            import io
            return io.StringIO()


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

    async def token_provider() -> str:
        return await auth.ensure_access_token(cfg)

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
        return ""
    except Exception as e:  # network/auth/etc — never crash
        sink.note(f"Error: {type(e).__name__}: {e}")
        return ""
    sink.end_turn(text)
    return text


def _default_shadow_factory(cfg, workspace: str):
    """The reversibility shadow git. Guarded like intel: any failure (no git
    binary, cache not writable) degrades to None -- coding still works, just
    without the time machine."""
    from webbee.checkpoints import ShadowGit, shadow_key
    from webbee.repo import find_repo_root
    root = find_repo_root(workspace)
    sg = ShadowGit(root, shadow_key(root), cache_dir=cfg.cache_dir)
    return sg if sg.ensure() else None


def _default_intel_factory(cfg, workspace: str):
    """Lazy/guarded -- a base install (no tree-sitter/watchfiles extra) must
    never fail to import here; `_boot` wraps the whole intel boot in
    try/except so any error (missing extra, indexing failure) degrades to
    `intel=None` rather than crashing the REPL."""
    from webbee.intel.service import IntelService
    from webbee.repo import compute_repo_key, find_repo_root
    root = find_repo_root(workspace)
    return IntelService(root, compute_repo_key(root), cache_dir=cfg.cache_dir)


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

    async def token_provider() -> str:
        return await auth.ensure_access_token(cfg)

    # Prod dock path = the default reader + a real tty + no injected sink; tests
    # inject sink/read_line and take the plain fallback loop.
    use_dock = sink is None and read_line is input and sys.stdin.isatty()
    state = {"mode": mode, "logged_in": False}
    _sink = None         # assigned by _boot
    agent = None         # assigned by _boot
    intel = None         # assigned by _boot -- IntelService, or None (off/base-install/boot failure)
    shadow = None        # assigned by _boot -- ShadowGit, or None (git unavailable / boot failure)
    watcher_task = None  # assigned by _boot -- background watchfiles task, cancelled on exit

    def _cycle() -> None:
        state["mode"] = next_mode(state["mode"])
        agent.mode = state["mode"]

    def _ctx() -> CommandContext:
        return CommandContext(mode=state["mode"], workspace=workspace, version=__version__,
                              surface="terminal", logged_in=state["logged_in"],
                              session_tokens=getattr(_sink, "session_tokens", 0),
                              session_credits=getattr(_sink, "session_credits", 0),
                              git_branch=state.get("git_branch", "-"))

    async def _handle(line: str) -> str:
        """Process one input line. Returns 'exit' or 'continue'."""
        if not line.strip():
            return "continue"
        res = dispatch(line, _ctx())
        if res.handled:
            if res.exit:
                return "exit"
            if res.action == "login":
                # ONE shared imperal_mcp mechanism: device-code flow (RFC 8628),
                # async, so we await it directly on the dock's event loop (the
                # /login turn runs as a background task, so the dock stays
                # responsive while it polls). on_prompt renders the code + URL
                # into the feed — a bare print would be invisible in the dock.
                def _login_prompt(user_code, uri, uri_complete):
                    show = getattr(_sink, "login_prompt", None)
                    if show:
                        show(user_code, uri)
                    else:
                        _sink.note(f"Open {uri} and enter code: {user_code}")
                email = await auth.login_device(cfg, on_prompt=_login_prompt)
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
        _sink.begin_turn()
        try:
            text = await agent.run(line, _sink, marathon=not once, goal=(line if not once else ""))
        except (KeyboardInterrupt, asyncio.CancelledError):
            _sink.abort()
            _sink.note("Interrupted.")
            return "continue"
        except Exception as e:  # network/auth/etc — never crash the REPL
            _sink.note(f"Error: {type(e).__name__}: {e}")
            return "continue"
        _sink.end_turn(text)
        return "continue"

    async def _boot(s) -> None:
        nonlocal _sink, agent, intel, watcher_task, shadow
        _sink = s
        # Cache git branch OFF the event loop (subprocess.run blocks it). Only
        # /status reads it; recomputing it per input line froze the dock.
        state["git_branch"] = await asyncio.to_thread(_git_branch, workspace)
        account = await account_fetcher(cfg, token_provider)
        state["logged_in"] = account.signed_in
        _sink.welcome(account, workspace, "terminal")
        # Boot replay of the durable per-user thread (Task 9): best-effort,
        # entirely swallowed on any failure -- history is a nice-to-have,
        # never a boot blocker (network down, no such session yet, etc.).
        try:
            from imperal_mcp.client import ImperalClient
            from webbee.thread import fetch_recent_thread, truncate_for_display
            _iid = await ImperalClient(cfg, token_provider).whoami()
            _msgs = await fetch_recent_thread(cfg, token_provider, f"marathon-{_iid}-rboot")
            for _m in _msgs[-40:]:
                _sink.foreign_turn(_m.get("surface", "terminal"), _m.get("role", ""),
                                   truncate_for_display(_m.get("content", "")))
            if _msgs:
                _sink.note("— live —")
        except Exception:
            pass  # replay is best-effort; never block boot
        if cfg.intel_enabled:
            # Off-loop build (indexing does sync file I/O + subprocess). Any
            # failure here (missing extra, bad repo, etc.) must degrade to
            # intel=None, never crash the boot -- coding still works, just
            # without repo intelligence.
            try:
                svc = (intel_factory or _default_intel_factory)(cfg, workspace)
                await asyncio.to_thread(svc.build)
                intel = svc
                from webbee.intel import watch
                watcher_task = asyncio.ensure_future(watch.watch_workspace(intel.root, intel.apply_changes))
            except Exception:
                intel = None
                watcher_task = None
        # Whole-mind P4: the reversibility shadow (never the user's VCS);
        # guarded -- boot must not fail over the time machine.
        try:
            shadow = await asyncio.to_thread(shadow_factory or _default_shadow_factory, cfg, workspace)
        except Exception:
            shadow = None
        agent = agent_factory(cfg, token_provider, workspace, state["mode"])

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
                    )
                finally:
                    if watcher_task is not None:
                        watcher_task.cancel()
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
        if watcher_task is not None:
            watcher_task.cancel()
