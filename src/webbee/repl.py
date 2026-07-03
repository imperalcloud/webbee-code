import asyncio
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


async def run_repl(cfg, mode: str = "default", *, sink=None, read_line=input,
                   agent_factory=None, auth=None, account_fetcher=None) -> None:
    """Interactive coding REPL. Non-slash lines go to the agent; slash lines
    are handled locally. Injectable deps (sink/read_line/agent_factory/auth/
    account_fetcher) exist for tests; production passes none."""
    if auth is None:
        from imperal_mcp import auth as _auth
        auth = _auth
    if sink is None:
        from webbee.render import RichSink
        sink = RichSink()
    if agent_factory is None:
        agent_factory = lambda c, tp, ws, m: AgentSession(c, tp, ws, m)  # noqa: E731
    if account_fetcher is None:
        from webbee.account import fetch_account as account_fetcher

    workspace = os.getcwd()

    async def token_provider() -> str:
        return await auth.ensure_access_token(cfg)

    account = await account_fetcher(cfg, token_provider)
    logged_in = account.signed_in
    sink.welcome(account, workspace, "terminal")

    agent = agent_factory(cfg, token_provider, workspace, mode)

    def _cycle() -> None:
        nonlocal mode
        mode = next_mode(mode)
        agent.mode = mode

    async def _read_line() -> "str | None":
        # Production (tty, default reader): the rich prompt_toolkit input.
        if read_line is input and sys.stdin.isatty():
            from webbee import tui
            return await tui.prompt(
                mode_getter=lambda: mode,
                usage_getter=lambda: (getattr(sink, "session_tokens", 0),
                                      getattr(sink, "session_cost", 0.0)),
                on_cycle=_cycle,
            )
        # Tests / non-tty: the injected (or builtin) sync reader.
        try:
            return read_line("❯ ")
        except (EOFError, KeyboardInterrupt):
            return None

    while True:
        line = await _read_line()
        if line is None:
            return
        if not line.strip():
            continue

        ctx = CommandContext(mode=mode, workspace=workspace, version=__version__,
                             surface="terminal", logged_in=logged_in,
                             session_tokens=getattr(sink, "session_tokens", 0),
                             session_cost=getattr(sink, "session_cost", 0.0),
                             git_branch=_git_branch(workspace))
        res = dispatch(line, ctx)

        if res.handled:
            if res.exit:
                return
            if res.action == "login":
                email = auth.login(cfg)
                logged_in = True
                sink.note(f"Signed in as {email}.")
                continue
            if res.action == "logout":
                await auth.logout(cfg)
                logged_in = False
                sink.note("Signed out, local credentials removed.")
                continue
            if res.action == "clear":
                sink.clear()
                sink.note(res.message)
                continue
            if res.action == "mode" and res.new_mode:
                mode = res.new_mode
                agent.mode = mode
            if res.message:
                sink.note(res.message)
            continue

        # A task for the agent.
        sink.user_echo(line)
        sink.begin_turn()
        try:
            text = await agent.run(line, sink)
        except (KeyboardInterrupt, asyncio.CancelledError):
            sink.abort()
            sink.note("Interrupted.")
            continue
        except Exception as e:  # network/auth/etc — never crash the REPL
            sink.note(f"Error: {type(e).__name__}: {e}")
            continue
        sink.end_turn(text)
