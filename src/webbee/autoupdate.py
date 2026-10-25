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
import tempfile
import time

DIST = "webbee"
MCP = "imperal-mcp"
MCP_SPEC = "imperal-mcp[code]"
MCP_GIT_SPEC = "imperal-mcp[code] @ git+https://github.com/imperalcloud/imperal-mcp.git"
PYPI = "https://pypi.org/pypi/{}/json"
OFF_ENV = "WEBBEE_NO_AUTOUPDATE"           # 1: never upgrade at the start (CI, a pinned setup)
DONE_ENV = "WEBBEE_UPGRADED"               # set on the restart after a self-upgrade: never twice in a row
LIGHT_ENV = "WEBBEE_LIGHT_PYTHON"          # the old Python whose light install runs this release (webbee._launch)
STEP_TIMEOUT_S = 300
# one upgrade at a time per user: two webbees started together reinstalled one venv at once and the restart found it
# half-written (live 2026-10-25: "No module named 'pygments.styles'"); the second waits for the first
LOCK_WAIT_S = 2 * STEP_TIMEOUT_S + 60
LOCK_STALE_S = 3 * STEP_TIMEOUT_S
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


def wheel_url(dist: str, version: str):
    """The release's own wheel on PyPI's file host -- there the moment it is published, while the index pages trail it
    by minutes (live 2026-10-25: pip found no 0.6.1 a minute after PyPI served it) -- or None."""
    try:
        import httpx
        r = httpx.get(PYPI.format(f"{dist}/{version}"), timeout=3.0)
        r.raise_for_status()
        wheels = [u["url"] for u in r.json().get("urls") or [] if u.get("packagetype") == "bdist_wheel"
                  and str(u.get("url") or "").startswith("https://files.pythonhosted.org/")]
        return wheels[0] if wheels else None
    except Exception:
        return None


def _spec(name: str, dist: str, version: str, wheel) -> str:
    """``name`` (with its extras) at ``version``: by its wheel's address when known, else by the index."""
    url = wheel(dist, version) if wheel else None
    return f"{name} @ {url}" if url else f"{name}=={version}"


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
                 uv=None, pipx=None, wheel=None):
    """The command that installs webbee ``version`` where this webbee is installed -- or None."""
    spec = _spec(DIST, DIST, version, wheel)
    light = env.get(LIGHT_ENV)
    if light:                               # an old Python runs this release through its light install
        return [light, "-m", "pip", "install", "--upgrade", "--no-cache-dir", spec]
    return _install(tool_of(prefix), spec, uv, pipx, executable, venv=DIST)


def _install(tool: str, spec: str, uv, pipx, python, venv: str = ""):
    """``spec`` installed by ``tool`` -- past its index cache: a release minutes old is missing from a cached index page
    (live 2026-10-25: pip listed nothing newer than 0.5.13 while PyPI served 0.6.0). A pipx venv that exists is upgraded
    in place (``runpip``): ``install --force`` deletes the venv and builds it again, and a start in that window found
    it half-written (live 2026-10-25)."""
    if tool == "uv" and uv:
        return [uv, "tool", "install", "--force", "--refresh", spec]
    if tool == "pipx" and pipx and venv:
        return [pipx, "runpip", venv, "install", "--upgrade", "--no-cache-dir", spec]
    if tool == "pipx" and pipx:
        return [pipx, "install", "--force", "--pip-args=--no-cache-dir", spec]
    if tool == "pip" and python:
        return [python, "-m", "pip", "install", "--upgrade", "--no-cache-dir", spec]
    return None


def mcp_installed(which=safe_which, run=subprocess.run) -> dict:
    """The shell's imperal-mcp (the command on PATH): ``{"tool", "python", "version", "code"}`` -- {} when none."""
    exe = which(MCP)
    if not exe:
        return {}
    real = os.path.realpath(exe)
    py = os.path.join(os.path.dirname(real), "python.exe" if _WINDOWS else "python")
    out = {"tool": tool_of(real), "python": py, "version": "", "code": False}
    # asked twice when it says "no code extra": a reinstall is never made on one odd answer (live 2026-10-25: right after
    # another install the first answer said no webbee, the next ones yes)
    for _ in range(2):
        try:
            got = run([py, "-c", _NO_CWD + "import importlib.metadata as m, importlib.util as u; "
                                 "print(m.version('imperal-mcp'), u.find_spec('webbee') is not None)"],
                      capture_output=True, text=True, timeout=20)
            version, code = (str(got.stdout or "").split() + ["", ""])[:2]
            out.update(version=version, code=code == "True")
        except Exception:
            pass
        if out["code"] or not out["version"]:
            break
    return out


def mcp_command(inst: dict, newest, uv, pipx, wheel=None):
    """The one command that makes the shell's imperal-mcp the newest with its code extra -- by the tool that installed
    it (uv, else pipx, for a new install) -- or None when it already is, or nothing can do it."""
    if inst and inst.get("version") and inst.get("version") == newest and inst.get("code"):
        return None
    if inst and not inst.get("version"):
        return None                         # it did not answer: a working install is never replaced blind
    if inst and not newest and inst.get("code"):
        return None                         # PyPI unread: what works stays
    spec = _spec(MCP_SPEC, MCP, newest, wheel) if newest else (MCP_SPEC if inst else MCP_GIT_SPEC)
    tool = inst.get("tool") if inst else ("uv" if uv else "pipx" if pipx else "")
    return _install(tool, spec, uv, pipx, inst.get("python") if inst else None, venv=MCP if inst else "")


def restart_command(argv, env=os.environ, executable: str = sys.executable):
    """How this webbee starts again on the release just installed, with the same arguments."""
    light = env.get(LIGHT_ENV)
    if light:
        return [light, "-c", _NO_CWD + "from webbee._launch import main; main()", *argv]
    return [executable, "-c", _NO_CWD + "from webbee.cli import main; main()", *argv]


def _lock_path() -> str:
    who = str(os.getuid()) if hasattr(os, "getuid") else (os.environ.get("USERNAME") or "user")
    return os.path.join(tempfile.gettempdir(), f"webbee-upgrade-{who}.lock")


def _lock(path: str) -> bool:
    """This start's turn to upgrade: the lock file made by it alone (``O_EXCL``, any system); a lock older than any
    upgrade takes is left over from a killed start and is taken over."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) < LOCK_STALE_S:
                    return False
                os.remove(path)
            except OSError:
                return False
        except OSError:
            return True                     # no place for a lock: upgrade as before rather than never
    return False


def _unlock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _wait_unlocked(path: str, sleep=time.sleep) -> None:
    end = time.time() + LOCK_WAIT_S
    while os.path.exists(path) and time.time() < end:
        sleep(1)


def imports_ok(python: str, run=subprocess.run) -> bool:
    """The installed webbee starts: its modules import in its own Python (a half-written venv fails here, not at the
    user's start)."""
    try:
        return run([python, "-c", _NO_CWD + "import webbee.cli, webbee.repl"], capture_output=True, text=True,
                   timeout=60).returncode == 0
    except Exception:
        return False


def _run(cmd, run=subprocess.run) -> bool:
    try:
        return run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                   timeout=STEP_TIMEOUT_S).returncode == 0
    except Exception:
        return False


def upgrade_before_start(argv, current: str, say=print, fetch=latest, which=safe_which, run=subprocess.run,
                         execv=os.execv, env=os.environ, wheel=wheel_url, lock_path=_lock_path,
                         sleep=time.sleep) -> list:
    """Bring webbee and the shell's imperal-mcp to their newest releases, then let the start go on; a self-upgrade
    starts webbee again on the new release (never returns then). Returns what it did, for the log. Never raises."""
    did: list = []
    lock = ""
    try:
        if env.get(OFF_ENV) == "1":
            return did
        lock = lock_path() if lock_path else ""
        if lock and not _lock(lock):
            # another webbee is upgrading this install: wait for it, then start on what it installed
            say("🐝 another webbee is updating -- waiting for it ...")
            _wait_unlocked(lock, sleep)
            lock = ""
            newest = fetch(DIST)
            if newest and _ver(newest) > _ver(current) and not env.get(DONE_ENV):
                env[DONE_ENV] = "1"
                again = restart_command(list(argv), env=env)
                if _WINDOWS:
                    sys.exit(subprocess.call(again))
                execv(again[0], again)
            return did
        uv, pipx = find_uv(which), which("pipx")
        newest = fetch(DIST)
        if newest and _ver(newest) > _ver(current) and not env.get(DONE_ENV):
            cmd = self_command(newest, env=env, uv=uv, pipx=pipx, wheel=wheel)
            if cmd:
                say(f"🐝 updating webbee {current} -> {newest} ...")
                python = env.get(LIGHT_ENV) or ""
                ok = _run(cmd, run) and (python or imports_ok(sys.executable, run) or
                                         (_run(cmd, run) and imports_ok(sys.executable, run)))
                if ok:
                    did.append(f"webbee {newest}")
                    env[DONE_ENV] = "1"           # the restart inherits it: never a second self-upgrade
                    _unlock(lock)                 # exec never returns: the lock goes before it, not in finally
                    lock = ""
                    again = restart_command(list(argv), env=env)
                    if _WINDOWS:            # no exec on Windows: run it and pass its exit code on
                        sys.exit(subprocess.call(again))
                    execv(again[0], again)
                    return did
                say(f"🐝 webbee could not update itself -- run: {' '.join(cmd)}")
        mcp_newest = fetch(MCP)
        inst = mcp_installed(which, run)
        cmd = mcp_command(inst, mcp_newest, uv, pipx, wheel)
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
    finally:
        if lock:
            _unlock(lock)
    return did

