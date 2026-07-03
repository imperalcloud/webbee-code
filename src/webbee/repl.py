import asyncio
import os
import subprocess

from webbee import __version__
from webbee.commands import CommandContext, dispatch
from webbee.session import AgentSession


def _git_branch(workspace: str) -> str:
    try:
        p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace,
                           capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else "-"
    except (OSError, subprocess.SubprocessError):
        return "-"


async def run_repl(cfg, mode: str = "default", *, sink=None, read_line=input,
                   agent_factory=None, auth=None) -> None:
    """Interactive coding REPL. Non-slash lines go to the agent; slash lines
    are handled locally. Injectable deps (sink/read_line/agent_factory/auth)
    exist for tests; production passes none."""
    if auth is None:
        from imperal_mcp import auth as _auth
        auth = _auth
    if sink is None:
        from webbee.render import RichSink
        sink = RichSink()
    if agent_factory is None:
        agent_factory = lambda c, tp, ws, m: AgentSession(c, tp, ws, m)  # noqa: E731

    workspace = os.getcwd()

    async def token_provider() -> str:
        return await auth.ensure_access_token(cfg)

    async def _logged_in() -> bool:
        try:
            await auth.ensure_access_token(cfg)
            return True
        except Exception:
            return False

    logged_in = await _logged_in()
    if not logged_in:
        sink.note("Ты не вошёл. Набери /login чтобы войти.")

    agent = agent_factory(cfg, token_provider, workspace, mode)

    while True:
        try:
            line = read_line("webbee> ")
        except (EOFError, KeyboardInterrupt):
            return
        if not line.strip():
            continue

        ctx = CommandContext(mode=mode, workspace=workspace, version=__version__,
                             surface="terminal", logged_in=logged_in,
                             tokens=getattr(sink, "tokens", 0),
                             cost_usd=getattr(sink, "cost_usd", 0.0),
                             git_branch=_git_branch(workspace))
        res = dispatch(line, ctx)

        if res.handled:
            if res.exit:
                return
            if res.action == "login":
                email = auth.login(cfg)
                logged_in = True
                sink.note(f"Вошёл как {email}.")
                continue
            if res.action == "logout":
                await auth.logout(cfg)
                logged_in = False
                sink.note("Вышел, локальные креды удалены.")
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
        sink.begin_turn()
        try:
            text = await agent.run(line, sink)
        except (KeyboardInterrupt, asyncio.CancelledError):
            sink.abort()
            sink.note("Прервано.")
            continue
        except Exception as e:  # network/auth/etc — never crash the REPL
            sink.note(f"Ошибка: {type(e).__name__}: {e}")
            continue
        sink.end_turn(text)
