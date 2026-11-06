"""Webbee starts on the newest release -- of itself and of imperal-mcp, the server every shell runs (Claude Code, Codex,
Gemini CLI) -- on any system (owner 2026-10-25: "при старте сначала сама делала апгрейд, а только потом стартовала";
"чтобы любой всегда все самые свежие пакеты имел").

pip has no install hook, so it happens at the start, before anything else: webbee upgrades itself by the tool that
installed it (a uv tool, a pipx venv, plain pip, or the light install that runs it on an old Python) and starts again on
the new release; then it brings the shell's imperal-mcp to its newest release WITH its code extra -- installed when
missing, upgraded by the tool that installed it (live 2026-10-25: an install had lost its code extra and the code tools
failed without a word). PyPI out of reach: nothing changes and webbee starts at once; nothing installed and PyPI out of
reach: imperal-mcp from its source, git. Every step is bounded and never fails the start.

The source is git (owner 2026-11-06: "Скрипт ВСЕГДА только на САМЫЙ свежий пакет смотреть должен ... не на pypi, а на
сам git repo"): webbee, imperal-mcp and imperal-sdk come from their repositories' ``stable`` branch, which each
release's CI moves to its tag. pipx and uv record what they installed and ``pipx reinstall`` / ``pipx upgrade`` /
``uv tool upgrade`` install that record again -- an install recorded as a release's own wheel address stayed on that
release forever (live 2026-11-06: ``pipx reinstall webbee`` put back 0.6.7 while 0.7.5 was out). The record is now the
branch, never a version: any of them brings the newest release, and an install still recorded by a version is moved
onto the branch once. No git (or GitHub out of reach), or the light install of an old Python (3.7 builds no package
from source): PyPI, as before."""
import os
import shutil
import subprocess
import sys
import time

DIST = "webbee"
MCP = "imperal-mcp"
SDK = "imperal-sdk"
MCP_SPEC = "imperal-mcp[code]"
SDK_SPEC = "imperal-sdk[universal-index]"
STABLE = "stable"                          # the branch CI moves to each release tag (never a version: see above)
REPOS = {DIST: "https://github.com/imperalcloud/webbee-code.git",
         MCP: "https://github.com/imperalcloud/imperal-mcp.git",
         SDK: "https://github.com/imperalcloud/imperal-sdk.git"}
NAMES = {DIST: DIST, MCP: MCP_SPEC, SDK: SDK_SPEC}   # each with the extras its install needs


def git_spec(dist: str, ref: str = STABLE, extras: bool = True) -> str:
    """``dist`` (with its extras) from its repository at ``ref`` -- the ``stable`` branch unless a tag is named."""
    return f"{NAMES[dist] if extras else dist} @ git+{REPOS[dist]}@{ref}"


MCP_GIT_SPEC = git_spec(MCP)
# imperal-mcp beside webbee goes WITHOUT its code extra: the extra requires webbee, and pipx (uv backend) installed
# that webbee from PyPI over the git one on a reinstall (live 2026-11-06: stable 0.7.6, `pipx reinstall` gave PyPI's
# 0.7.5). webbee's own dependencies already hold what the extra brings (tree-sitter, the sdk's index).
MCP_BESIDE_WEBBEE = git_spec(MCP, extras=False)
WEBBEE_ENV_SPECS = (git_spec(DIST), MCP_BESIDE_WEBBEE, git_spec(SDK))     # webbee's own environment
MCP_ENV_SPECS = (MCP_GIT_SPEC, git_spec(SDK), git_spec(DIST))             # the shell's imperal-mcp
PYPI = "https://pypi.org/pypi/{}/json"
OFF_ENV = "WEBBEE_NO_AUTOUPDATE"           # 1: never upgrade at the start (CI, a pinned setup)
DONE_ENV = "WEBBEE_UPGRADED"               # set on the restart after a self-upgrade: never twice in a row
LIGHT_ENV = "WEBBEE_LIGHT_PYTHON"          # the old Python whose light install runs this release (webbee._launch)
STEP_TIMEOUT_S = 300
# one upgrade at a time per user: two webbees started together reinstalled one venv at once and the restart found it
# half-written (live 2026-10-25: "No module named 'pygments.styles'"); the second waits for the first -- a bounded
# while, then starts without upgrading. The lock names its holder's pid: a holder gone (Ctrl-C, a crash) frees it at
# once; a lock older than any upgrade takes (two installs, the way back, imperal-mcp) is stale even if its pid lives on
# in another program. The wait stays well under the stale age: a waiter never sits out a dead lock.
LOCK_WAIT_S = 120
LOCK_STALE_S = 5 * STEP_TIMEOUT_S
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


def stable_version(dist: str, git, run=subprocess.run):
    """The release ``dist``'s ``stable`` branch stands on -- the version tag on its commit, read from the repository
    itself (no index that trails a release) -- or None: no git, GitHub out of reach, no branch or no tag on it."""
    if not git:
        return None
    try:
        got = run([git, "ls-remote", REPOS[dist], f"refs/heads/{STABLE}", "refs/tags/v*"], capture_output=True,
                  text=True, timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if got.returncode != 0:
            return None
        head, direct, peeled = "", {}, {}
        for line in str(got.stdout or "").splitlines():
            sha, _, ref = line.strip().partition("\t")
            if ref == f"refs/heads/{STABLE}":
                head = sha
            elif ref.startswith("refs/tags/v"):
                name = ref[len("refs/tags/v"):]
                if name.endswith("^{}"):
                    peeled[name[:-3]] = sha     # the commit an annotated tag names
                else:
                    direct[name] = sha
        on_head = [v for v, sha in direct.items() if peeled.get(v, sha) == head and _ver(v)]
        return max(on_head, key=_ver) if head and on_head else None
    except Exception:
        return None


def on_stable(tool: str, prefix: str, specs) -> bool:
    """What pipx / uv will install again for the environment at ``prefix`` (``pipx reinstall``, ``pipx upgrade``,
    ``uv tool upgrade``) is ``specs``, each by its repository's stable branch. pipx: each spec exactly as given (an
    imperal-mcp recorded beside webbee WITH its code extra pulled webbee from PyPI on a reinstall); uv resolves the
    record as one set, so its repository and branch suffice. Plain pip keeps no such record."""
    try:
        if tool == "pipx":
            import json
            with open(os.path.join(prefix, "pipx_metadata.json"), encoding="utf-8") as f:
                meta = json.load(f)
            recorded = [str((meta.get("main_package") or {}).get("package_or_url") or "")]
            recorded += [str((p or {}).get("package_or_url") or "") for p in (meta.get("injected_packages") or {}).values()]
            return all(s in recorded for s in specs)
        if tool == "uv":
            with open(os.path.join(prefix, "uv-receipt.toml"), encoding="utf-8") as f:
                receipt = f.read()
            urls = [s.split(" @ git+", 1)[-1].rsplit("@", 1)[0] for s in specs]
            return all(f'git = "{u}?rev={STABLE}"' in receipt for u in urls)
    except Exception:
        return False
    return True


def _git_install(tool: str, main: str, withs, uv, pipx, python, venv: str, record: bool):
    """``main`` and ``withs`` (the packages beside it) from git, by ``tool``, recorded as given. pipx: an environment
    already recorded on the branch is upgraded by its own record (``upgrade --include-injected``, in place); otherwise
    (``record``: a new one, or one recorded by a version) installed again and its packages injected -- the only way
    pipx writes a new record. uv records ``--with`` beside the tool on every install."""
    if tool == "uv" and uv:
        cmd = [uv, "tool", "install", "--force", "--refresh"]
        for w in withs:
            cmd += ["--with", w]
        return cmd + [main]
    if tool == "pipx" and pipx:
        if not record:
            return [pipx, "upgrade", "--include-injected", venv]
        return [[pipx, "install", "--force", main], [pipx, "inject", "--force", venv, *withs]]
    if tool == "pip" and python:
        return [python, "-m", "pip", "install", "--upgrade", "--no-cache-dir", main, *withs]
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
                 uv=None, pipx=None, wheel=None, source: str = "pypi", record: bool = True, ref: str = STABLE):
    """The command that installs webbee ``version`` where this webbee is installed -- or None. ``source`` "git": from
    the repositories at ``ref`` (the stable branch; a tag only to put a release back), imperal-mcp and imperal-sdk
    beside it; ``record``: pipx writes its record anew (see ``_git_install``)."""
    light = env.get(LIGHT_ENV)
    if source == "git" and not light:
        return _git_install(tool_of(prefix), git_spec(DIST, ref), [MCP_BESIDE_WEBBEE, git_spec(SDK)], uv, pipx,
                            executable, DIST, record)
    spec = _spec(DIST, DIST, version, wheel)
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
    """The shell's imperal-mcp (the command on PATH): ``{"tool", "python", "prefix", "version", "code"}`` -- {} when
    none."""
    exe = which(MCP)
    if not exe:
        return {}
    real = os.path.realpath(exe)
    py = os.path.join(os.path.dirname(real), "python.exe" if _WINDOWS else "python")
    out = {"tool": tool_of(real), "python": py, "prefix": os.path.dirname(os.path.dirname(real)), "version": "",
           "code": False, "webbee": ""}
    # asked twice when it says "no code extra": a reinstall is never made on one odd answer (live 2026-10-25: right after
    # another install the first answer said no webbee, the next ones yes)
    for _ in range(2):
        try:
            got = run([py, "-c", _NO_CWD + "import importlib.metadata as m, importlib.util as u; "
                                 "w = u.find_spec('webbee') is not None; "
                                 "print(m.version('imperal-mcp'), w, m.version('webbee') if w else '')"],
                      capture_output=True, text=True, timeout=20)
            version, code, webbee = (str(got.stdout or "").split() + ["", "", ""])[:3]
            out.update(version=version, code=code == "True", webbee=webbee)
        except Exception:
            pass
        if out["code"] or not out["version"]:
            break
    return out


def mcp_command(inst: dict, newest, uv, pipx, wheel=None, source: str = "pypi", webbee_newest=None):
    """The one command that makes the shell's imperal-mcp the newest with its code extra -- by the tool that installed
    it (uv, else pipx, for a new install) -- or None when it already is, or nothing can do it. ``source`` "git": from
    the stable branches, imperal-sdk and webbee beside it, and an install recorded by a version is moved onto the
    branch even when its version is the newest (what ``pipx reinstall`` / ``uv tool upgrade`` would put back). The
    webbee beside it (its code tools) follows webbee's own releases: one newer than it holds upgrades the environment
    too (live 2026-11-06: webbee 0.7.7 out, imperal-mcp unchanged, its environment kept webbee 0.7.6)."""
    git = source == "git"
    recorded = bool(inst) and on_stable(inst.get("tool") or "", inst.get("prefix") or "", MCP_ENV_SPECS)
    behind = git and bool(inst) and bool(inst.get("webbee")) and bool(webbee_newest) \
        and _ver(inst.get("webbee")) < _ver(webbee_newest)
    if inst and inst.get("version") and inst.get("version") == newest and inst.get("code") and (recorded or not git) \
            and not behind:
        return None
    if inst and not inst.get("version"):
        return None                         # it did not answer: a working install is never replaced blind
    if inst and not newest and inst.get("code"):
        return None                         # PyPI unread: what works stays
    if git and inst and _ver(inst.get("version")) > _ver(newest):
        return None                         # newer than the last release (a build of its own): never taken back
    tool = inst.get("tool") if inst else ("uv" if uv else "pipx" if pipx else "")
    if git:
        return _git_install(tool or "", MCP_GIT_SPEC, [git_spec(SDK), git_spec(DIST)], uv, pipx,
                            inst.get("python") if inst else None, MCP, record=not recorded)
    spec = _spec(MCP_SPEC, MCP, newest, wheel) if newest else (MCP_SPEC if inst else MCP_GIT_SPEC)
    return _install(tool or "", spec, uv, pipx, inst.get("python") if inst else None, venv=MCP if inst else "")


def restart_command(argv, env=os.environ, executable: str = sys.executable):
    """How this webbee starts again on the release just installed, with the same arguments."""
    light = env.get(LIGHT_ENV)
    if light:
        return [light, "-c", _NO_CWD + "from webbee._launch import main; main()", *argv]
    return [executable, "-c", _NO_CWD + "from webbee.cli import main; main()", *argv]


def _lock_path() -> str:
    """The lock in webbee's own data folder of this user (0700) -- never a shared temp folder, where anyone could make
    it first and hold every upgrade back (security review 2026-10-25)."""
    try:
        from webbee._launch import _home
        base = os.path.dirname(_home())
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".webbee")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, "upgrade.lock")


def _pid_alive(pid: int) -> bool:
    """The process ``pid`` still runs -- True when it cannot be told (a lock is never taken from a live upgrader on a
    guess)."""
    if pid <= 0:
        return True
    if pid == os.getpid():
        return True
    if _WINDOWS:                            # os.kill on Windows terminates: ask the process table instead
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return k32.GetLastError() != 87             # ERROR_INVALID_PARAMETER: no such process
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return not ok or code.value == 259              # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                         # EPERM: alive, another user's
    return True


def _stale(path: str) -> bool:
    """A lock left over: its holder's pid is gone, or it is older than any upgrade takes."""
    try:
        if time.time() - os.stat(path).st_mtime >= LOCK_STALE_S:
            return True
        with open(path) as f:
            head = (f.read(64).split() or [""])[0]
    except OSError:
        return False
    return head.isdigit() and not _pid_alive(int(head))    # being written (empty): not stale


def _lock(path: str) -> bool:
    """This start's turn to upgrade: the lock file made by it alone (``O_EXCL``, any system), holding its pid and start
    time; a lock whose holder is gone, or older than any upgrade takes, is left over from a killed start and taken
    over at once."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                st = os.stat(path)
                if hasattr(os, "getuid") and st.st_uid != os.getuid():
                    return True             # not this user's: no lock of ours, the upgrade goes on
                if not _stale(path):
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


def _wait_unlocked(path: str, sleep=time.sleep) -> str:
    """Wait for the upgrade another webbee is making: "free" when it let go, "stale" when its holder died, "busy" when
    it still holds after LOCK_WAIT_S (counted in one-second sleeps: bounded however the clock moves)."""
    for _ in range(LOCK_WAIT_S):
        if not os.path.exists(path):
            return "free"
        if _stale(path):
            return "stale"
        sleep(1)
    return "free" if not os.path.exists(path) else "busy"


def imports_ok(python: str, run=subprocess.run, light: bool = False) -> bool:
    """The installed webbee starts: its modules import in its own Python (a half-written venv fails here, not at the
    user's start). A light install is checked as its old Python runs it: the launcher (``webbee._launch``), which then
    sets up that release's runtime on its first start."""
    mods = "import webbee, webbee._launch" if light else "import webbee.cli, webbee.repl"
    try:
        return run([python, "-c", _NO_CWD + mods], capture_output=True, text=True, timeout=60).returncode == 0
    except Exception:
        return False


def _steps(cmd) -> list:
    """One command, or the commands of one install in their order (pipx: install, then inject)."""
    return [list(c) for c in cmd] if cmd and isinstance(cmd[0], (list, tuple)) else [list(cmd or [])]


def _show(cmd) -> str:
    """What a person runs by hand to do ``cmd``."""
    return " && ".join(" ".join(f"'{a}'" if " " in a or "[" in a else a for a in c) for c in _steps(cmd))


def _run(cmd, run=subprocess.run) -> bool:
    """Every step of ``cmd`` in its order; the first that fails stops the rest."""
    try:
        for step in _steps(cmd):
            if run(step, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                   timeout=STEP_TIMEOUT_S).returncode != 0:
                return False
        return True
    except Exception:
        return False


def _newest(dist: str, git, fetch, stable) -> tuple:
    """``(version, source)``: the release on ``dist``'s stable branch ("git") when git reads it, else PyPI's."""
    v = stable(dist, git) if git else None
    return (v, "git") if v else (fetch(dist), "pypi")


def _roll_back(current: str, newest: str, python: str, light: str, env, uv, pipx, wheel, run, say,
               source: str = "pypi", prefix: str = sys.prefix) -> bool:
    """``newest`` installed but does not import: ``current`` installed again by the same tool and checked the same
    way -- never a release on disk that does not start. Says what happened either way. From git: ``current``'s own
    tag (recorded so until the next release moves it onto the branch again)."""
    back = self_command(current, prefix=prefix, env=env, uv=uv, pipx=pipx, wheel=wheel, source=source,
                        ref=f"v{current}")
    if back and _run(back, run) and imports_ok(python, run, light=bool(light)):
        say(f"🐝 webbee {newest} was installed but does not start (its modules do not import) -- "
            f"put {current} back; starting {current}")
        return True
    fix = _show(back) if back else f"pip install webbee=={current}"
    say(f"🐝 webbee {newest} was installed but does not start, and {current} could not be put back -- "
        f"this session runs on, but the next start may fail: run {fix}")
    return False


def upgrade_before_start(argv, current: str, say=print, fetch=latest, which=safe_which, run=subprocess.run,
                         execv=os.execv, env=os.environ, wheel=wheel_url, lock_path=_lock_path,
                         sleep=time.sleep, stable=stable_version, prefix: str = sys.prefix) -> list:
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
            state = _wait_unlocked(lock, sleep)
            if state == "stale":            # its upgrader died midway: this start makes the upgrade (and mends it)
                state = "taken" if _lock(lock) else "busy"
            if state == "busy":             # never longer than LOCK_WAIT_S: start now, on this release
                lock = ""
                say(f"🐝 another webbee is still updating -- starting {current} without updating")
                return did
            if state == "free":
                lock = ""
                newest = _newest(DIST, None if env.get(LIGHT_ENV) else which("git"), fetch, stable)[0]
                if newest and _ver(newest) > _ver(current) and not env.get(DONE_ENV):
                    env[DONE_ENV] = "1"
                    again = restart_command(list(argv), env=env)
                    if _WINDOWS:
                        sys.exit(subprocess.call(again))
                    execv(again[0], again)
                return did
        uv, pipx = find_uv(which), which("pipx")
        light = env.get(LIGHT_ENV) or ""
        git = None if light else which("git")     # 3.7's pip builds no package from source: the light install stays on PyPI
        newest, source = _newest(DIST, git, fetch, stable)
        tool = tool_of(prefix)
        recorded = source != "git" or on_stable(tool, prefix, WEBBEE_ENV_SPECS)
        # the same release recorded by a version: moved onto the branch (what pipx/uv would put back on a reinstall)
        if newest and (_ver(newest) > _ver(current) or (not recorded and _ver(newest) == _ver(current))) \
                and not env.get(DONE_ENV):
            cmd = self_command(newest, prefix=prefix, env=env, uv=uv, pipx=pipx, wheel=wheel, source=source,
                               record=not recorded)
            if cmd:
                say(f"🐝 updating webbee {current} -> {newest} ..." if _ver(newest) > _ver(current) else
                    f"🐝 moving webbee {current} onto its release branch, so a reinstall brings the newest ...")
                python = light or sys.executable
                installed = _run(cmd, run)
                ok = installed and (imports_ok(python, run, light=bool(light)) or
                                    (_run(cmd, run) and imports_ok(python, run, light=bool(light))))
                if not ok and (installed or not imports_ok(python, run, light=bool(light))):
                    # on disk a release that does not import (or an install that broke off halfway): the next start
                    # would run it and stay stuck. The one running now (and any other open session) holds the old
                    # modules: put the old release back
                    _roll_back(current, newest, python, light, env, uv, pipx, wheel, run, say, source, prefix)
                elif ok:
                    did.append(f"webbee {newest}")
                    env[DONE_ENV] = "1"           # the restart inherits it: never a second self-upgrade
                    _unlock(lock)                 # exec never returns: the lock goes before it, not in finally
                    lock = ""
                    again = restart_command(list(argv), env=env)
                    if _WINDOWS:            # no exec on Windows: run it and pass its exit code on
                        sys.exit(subprocess.call(again))
                    execv(again[0], again)
                    return did
                else:
                    say(f"🐝 webbee could not update itself -- run: {_show(cmd)}")
        mcp_newest, mcp_source = _newest(MCP, git, fetch, stable)
        inst = mcp_installed(which, run)
        cmd = mcp_command(inst, mcp_newest, uv, pipx, wheel, source=mcp_source,
                          webbee_newest=newest if source == "git" else None)
        if cmd:
            what = (f"imperal-mcp {inst.get('version') or '?'} -> {mcp_newest or 'source'}"
                    + (f", webbee beside it {inst.get('webbee')} -> {newest}"
                       if source == "git" and inst.get("webbee") and _ver(inst.get("webbee")) < _ver(newest) else "")
                    if inst
                    else f"imperal-mcp {mcp_newest or 'from source'}") + (" (git, stable)" if mcp_source == "git" else "")
            say(f"🐝 updating {what} ...")
            if _run(cmd, run):
                did.append(what)
            else:
                say(f"🐝 imperal-mcp could not be updated -- run: {_show(cmd)}")
    except SystemExit:
        raise
    except Exception:
        pass
    finally:
        if lock:
            _unlock(lock)
    return did

