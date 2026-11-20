"""The three doors the field gate goes through -- always the installed products, never a re-assembly of their parts.

- McpDriver: launches the installed `imperal-mcp` console script and talks MCP over stdio, as Claude Code does.
- WebbeeDriver: webbee's own workspace boot (boot.boot_workspace) and executor (LocalToolExecutor), as session.py
  builds them for every turn.
- hook(): `imperal-mcp hook <event>` with the shell's JSON on stdin, as Claude Code runs its hooks.

Each driver gets its own HOME, so a run never reads the machine's caches, credentials or spool.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import AsyncExitStack
from pathlib import Path

CALL_TIMEOUT_S = 120


class GateError(RuntimeError):
    """A driver could not reach the product: the gate stops loudly, never records it as a pass."""


def _home_env(home: Path, token: str | None = None) -> dict:
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "WEBBEE_NO_AUTOUPDATE": "1",
           "IMPERAL_INTEL_CACHE_DIR": str(home / ".cache" / "webbee" / "intel"), "PYTHONUTF8": "1"}
    env.pop("IMPERAL_TOKEN", None)
    if token:
        env["IMPERAL_TOKEN"] = token
    return env


def webbee_argv() -> list:
    """How the gate starts the client: the ``webbee`` package THIS Python imports, as a module. The console script
    cannot be used for it: an installer writes it with ``python -E``, which ignores PYTHONPATH -- so a gate run from
    a source tree started the INSTALLED client and judged that one (found 2026-11-20: every wait scenario of the
    branch read "end: not said", the released client knew no such line)."""
    import sys
    return [sys.executable, "-m", "webbee"]


def console(name: str) -> str:
    """The console script of the Python running the gate first -- the packages under test -- then PATH (2026-11-15:
    PATH found the user's own ~/.local/bin/imperal-mcp, a released version, and the gate judged that one)."""
    import sys
    here = Path(sys.executable).parent
    for cand in (here / name, here / f"{name}.exe", here / "Scripts" / f"{name}.exe"):
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    if not found:
        raise GateError(f"the installed `{name}` console script is not on PATH")
    return found


class McpDriver:
    def __init__(self, root: Path, home: Path, token: str | None = None):
        self.root, self.home, self.token = Path(root), Path(home), token
        self._stack: AsyncExitStack | None = None
        self._session = None

    async def __aenter__(self) -> "McpDriver":
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=console("imperal-mcp"), args=[], cwd=str(self.root),
                                       env=_home_env(self.home, self.token))
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(self._session.initialize(), CALL_TIMEOUT_S)
        except Exception as exc:
            await self._stack.aclose()
            raise GateError(f"imperal-mcp did not start over stdio: {type(exc).__name__}: {exc}") from exc
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def list_tools(self) -> list[str]:
        res = await asyncio.wait_for(self._session.list_tools(), CALL_TIMEOUT_S)
        return [t.name for t in res.tools]

    async def call(self, tool: str, args: dict) -> dict:
        res = await asyncio.wait_for(self._session.call_tool(tool, args), CALL_TIMEOUT_S)
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        data = None
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            pass
        return {"ok": not res.isError, "text": text, "data": data if isinstance(data, dict) else None}


class WebbeeDriver:
    def __init__(self, root: Path, home: Path):
        self.root, self.home = Path(root), Path(home)
        self._bundle: dict | None = None
        self._executor = None
        self._saved_env: dict | None = None

    async def __aenter__(self) -> "WebbeeDriver":
        self._saved_env = dict(os.environ)
        env = _home_env(self.home)              # built from the current environment BEFORE it is replaced
        os.environ.clear()
        os.environ.update(env)
        from imperal_mcp import auth
        from webbee import boot
        from webbee.config import Config
        from webbee.session import AgentSession
        from webbee.tokens import make_token_provider
        from webbee.tools import LocalToolExecutor
        cfg = Config.from_env()
        self._bundle = await boot.boot_workspace(cfg, str(self.root), None, None)
        # as session.py builds it for every turn: the same intel, shadow and cloud client (the vault graph needs it)
        session = AgentSession(cfg, make_token_provider(cfg, auth), str(self.root), "default",
                               intel=self._bundle["intel"], shadow=self._bundle["shadow"])
        self._executor = LocalToolExecutor(str(self.root), indexer=self._bundle["intel"],
                                           shadow=self._bundle["shadow"],
                                           client_factory=session._file_client_factory())
        return self

    async def __aexit__(self, *exc) -> None:
        task = (self._bundle or {}).get("watcher_task")
        if task is not None:
            task.cancel()
        intel = (self._bundle or {}).get("intel")
        if intel is not None and hasattr(intel, "close"):
            # as webbee quits: its code sync ends while this loop still runs (a sync left behind was cancelled
            # mid-request and printed a traceback after the last scenario, 2026-11-15)
            await asyncio.to_thread(intel.close)
        if self._saved_env is not None:
            os.environ.clear()
            os.environ.update(self._saved_env)

    async def run(self, tool: str, args: dict) -> dict:
        return await asyncio.wait_for(asyncio.to_thread(self._executor.run, tool, args), CALL_TIMEOUT_S)


def hook(event: str, payload: dict, home: Path, token: str | None = None) -> dict:
    proc = subprocess.run([console("imperal-mcp"), "hook", event], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=CALL_TIMEOUT_S,
                          cwd=payload.get("cwd") or None, env=_home_env(Path(home), token))
    out = None
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        pass
    return {"code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "json": out}
