"""Webbee starts on the newest release -- of itself and of imperal-mcp, the server every shell runs (Claude Code, Codex,
Gemini CLI) -- on any system (owner 2026-10-25: "при старте сначала сама делала апгрейд, а только потом стартовала";
"чтобы любой всегда все самые свежие пакеты имел").

pip has no install hook, so it happens at the start, before anything else: webbee upgrades itself by the tool that
installed it (a uv tool, a pipx venv, plain pip, or the light install that runs it on an old Python) and starts again on
the new release; then it brings the shell's imperal-mcp to its newest release WITH its code extra -- installed when
missing, upgraded by the tool that installed it (live 2026-10-25: an install had lost its code extra and the code tools
failed without a word). PyPI out of reach: nothing changes and webbee starts at once; nothing installed and PyPI out of
reach: imperal-mcp from its source, git. Every step is bounded and never fails the start."""
import os
import shutil
import subprocess
import sys

DIST = "webbee"
MCP = "imperal-mcp"
MCP_SPEC = "imperal-mcp[code]"
MCP_GIT_SPEC = "imperal-mcp[code] @ git+https://github.com/imperalcloud/imperal-mcp.git"
PYPI = "https://pypi.org/pypi/{}/json"
OFF_ENV = "WEBBEE_NO_AUTOUPDATE"           # 1: never upgrade at the start (CI, a pinned setup)
DONE_ENV = "WEBBEE_UPGRADED"               # set on the restart after a self-upgrade: never twice in a row
LIGHT_ENV = "WEBBEE_LIGHT_PYTHON"          # the old Python whose light install runs this release (webbee._launch)
STEP_TIMEOUT_S = 300
_WINDOWS = os.name == "nt"
# `python -c` puts the current folder first on sys.path: a webbee.py there would run in our place (security review).
# Dropped before any import -- `-I` is no way out, it also drops a `pip install --user` webbee.
_NO_CWD = "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "


def safe_which(name: str, which=shutil.which):
    """A program from PATH, never one sitting in the current folder unless that folder is on PATH itself (Windows looks
    in the current folder first -- security review)."""
    found = which(name)
    if not found:
        return None
    here = os.path.abspath(os.getcwd())
    on_path = {os.path.abspath(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p and os.path.isabs(p)}
    if os.path.dirname(os.path.abspath(found)) == here and here not in on_path:
        return None
    return found


def latest(dist: str):
    """The newest release of ``dist`` on PyPI, or None when PyPI cannot be read (offline, a proxy, a timeout)."""
    try:
        import httpx
        r = httpx.get(PYPI.format(dist), timeout=3.0)
        r.raise_for_status()
        return str(r.json()["info"]["version"])
    except Exception:
        return None


def _ver(s) -> tuple:
    try:
        return tuple(int(x) for x in str(s).strip().split("."))
    except (ValueError, AttributeError):
        return ()


def tool_of(path: str) -> str:
    """Which tool owns the environment at ``path``: uv (a uv tool), pipx (its venv), or pip."""
    p = str(path or "").replace("\\", "/")
    if "/uv/tools/" in p:
        return "uv"
    if "/pipx/venvs/" in p:
        return "pipx"
    return "pip"


def find_uv(which=safe_which):
    try:
        from webbee._launch import _find_uv, _home
        return _find_uv(_home(), which)
    except Exception:
        return which("uv")


def self_command(version: str, prefix: str = sys.prefix, executable: str = sys.executable, env=os.environ,
                 uv=None, pipx=None):
    """The command that installs webbee ``version`` where this webbee is installed -- or None."""
    spec = f"{DIST}=={version}"
    light = env.get(LIGHT_ENV)
    if light:                               # an old Python runs this release through its light install
        return [light, "-m", "pip", "install", "--upgrade", spec]
    tool = tool_of(prefix)
    if tool == "uv" and uv:
        return [uv, "tool", "install", "--force", spec]
    if tool == "pipx" and pipx:
        return [pipx, "install", "--force", spec]
    if tool == "pip":
        return [executable, "-m", "pip", "install", "--upgrade", spec]
    return None


def mcp_installed(which=safe_which, run=subprocess.run) -> dict:
    """The shell's imperal-mcp (the command on PATH): ``{"tool", "python", "version", "code"}`` -- {} when none."""
    exe = which(MCP)
    if not exe:
        return {}
    real = os.path.realpath(exe)
    py = os.path.join(os.path.dirname(real), "python.exe" if _WINDOWS else "python")
    out = {"tool": tool_of(real), "python": py, "version": "", "code": False}
    try:
        got = run([py, "-c", _NO_CWD + "import importlib.metadata as m, importlib.util as u; "
                             "print(m.version('imperal-mcp'), u.find_spec('webbee') is not None)"],
                  capture_output=True, text=True, timeout=20)
        version, code = (str(got.stdout or "").split() + ["", ""])[:2]
        out.update(version=version, code=code == "True")
    except Exception:
        pass
    return out


def mcp_command(inst: dict, newest, uv, pipx):
    """The one command that makes the shell's imperal-mcp the newest with its code extra -- by the tool that installed
    it (uv, else pipx, for a new install) -- or None when it already is, or nothing can do it."""
    if inst and inst.get("version") and inst.get("version") == newest and inst.get("code"):
        return None
    if inst and not newest and inst.get("code"):
        return None                         # PyPI unread: what works stays
    spec = f"{MCP_SPEC}=={newest}" if newest else (MCP_SPEC if inst else MCP_GIT_SPEC)
    tool = inst.get("tool") if inst else ("uv" if uv else "pipx" if pipx else "")
    if tool == "uv" and uv:
        return [uv, "tool", "install", "--force", spec]
    if tool == "pipx" and pipx:
        return [pipx, "install", "--force", spec]
    if tool == "pip" and inst.get("python"):
        return [inst["python"], "-m", "pip", "install", "--upgrade", spec]
    return None


def restart_command(argv, env=os.environ, executable: str = sys.executable):
    """How this webbee starts again on the release just installed, with the same arguments."""
    light = env.get(LIGHT_ENV)
    if light:
        return [light, "-c", _NO_CWD + "from webbee._launch import main; main()", *argv]
    return [executable, "-c", _NO_CWD + "from webbee.cli import main; main()", *argv]


def _run(cmd, run=subprocess.run) -> bool:
    try:
        return run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                   timeout=STEP_TIMEOUT_S).returncode == 0
    except Exception:
        return False


def upgrade_before_start(argv, current: str, say=print, fetch=latest, which=safe_which, run=subprocess.run,
                         execv=os.execv, env=os.environ) -> list:
    """Bring webbee and the shell's imperal-mcp to their newest releases, then let the start go on; a self-upgrade
    starts webbee again on the new release (never returns then). Returns what it did, for the log. Never raises."""
    did: list = []
    try:
        if env.get(OFF_ENV) == "1":
            return did
        uv, pipx = find_uv(which), which("pipx")
        newest = fetch(DIST)
        if newest and _ver(newest) > _ver(current) and not env.get(DONE_ENV):
            cmd = self_command(newest, env=env, uv=uv, pipx=pipx)
            if cmd:
                say(f"🐝 updating webbee {current} -> {newest} ...")
                if _run(cmd, run):
                    did.append(f"webbee {newest}")
                    env[DONE_ENV] = "1"           # the restart inherits it: never a second self-upgrade
                    again = restart_command(list(argv), env=env)
                    if _WINDOWS:            # no exec on Windows: run it and pass its exit code on
                        sys.exit(subprocess.call(again))
                    execv(again[0], again)
                    return did
                say(f"🐝 webbee could not update itself -- run: {' '.join(cmd)}")
        mcp_newest = fetch(MCP)
        inst = mcp_installed(which, run)
        cmd = mcp_command(inst, mcp_newest, uv, pipx)
        if cmd:
            what = (f"imperal-mcp {inst.get('version') or '?'} -> {mcp_newest or 'source'}" if inst
                    else f"imperal-mcp {mcp_newest or 'from source'}")
            say(f"🐝 updating {what} ...")
            if _run(cmd, run):
                did.append(what)
            else:
                say(f"🐝 imperal-mcp could not be updated -- run: {' '.join(cmd)}")
    except SystemExit:
        raise
    except Exception:
        pass
    return did

