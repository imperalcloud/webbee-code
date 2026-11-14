from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/webbee/json"


def _ver(s: str) -> tuple:
    """A release's numbers ("0.7.16" -> (0, 7, 16)); a suffix (rc1, .dev0, +local) is not part of them; () when none
    (live 2026-11-14: any suffix made it (), and "newer than ()" said an update was out to someone on the newest)."""
    import re
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(s or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def default_fetch() -> "str | None":
    """The newest release by the SAME source the start's self-upgrade and ``pipx``/``uv tool upgrade`` install from:
    webbee's ``stable`` branch (git), PyPI only without git (live 2026-11-14: the notice read PyPI while the upgrade
    installed from ``stable`` -- whenever the two stood apart, it said "v... available" to someone the upgrade had
    just put on the newest). None on ANY failure -- the caller treats None as 'no update'."""
    try:
        import os
        from webbee import autoupdate as au
        git = None if os.environ.get(au.LIGHT_ENV) else au.safe_which("git")
        v = au.stable_version(au.DIST, git) if git else None
        if v:
            return v
    except Exception:
        pass
    try:
        import httpx
        r = httpx.get(PYPI_URL, timeout=2.0)
        r.raise_for_status()
        return r.json()["info"]["version"]
    except Exception:
        return None


def _resolve_latest(current: str, *, cache_path, now: float, fetch, ttl: float) -> "str | None":
    """The shared cache-then-fetch resolution: the latest version string we
    know about, or None when we genuinely could not find out (offline, bad
    response, unwritable cache). Never raises."""
    cache_path = Path(cache_path)
    latest = None
    try:
        import json
        cached = json.loads(cache_path.read_text())
        if now - float(cached.get("checked_at", 0)) < ttl:
            latest = cached.get("latest")
    except Exception:
        latest = None

    if latest is None:
        try:
            latest = fetch()
        except Exception:
            latest = None
        if latest:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"latest": latest, "checked_at": now}))
            except Exception:
                pass
    return latest


def upgrade_hint(prefix: str = "", executable: str = "") -> str:
    """The upgrade command for THIS install, by the tool that made it (live 2026-11-14: a plain pip install on
    Windows was told "pipx upgrade webbee", which upgrades nothing it has)."""
    import sys
    from webbee.autoupdate import tool_of
    tool = tool_of(prefix or sys.prefix)
    if tool == "uv":
        return "uv tool upgrade webbee"
    if tool == "pipx":
        return "pipx upgrade webbee"
    return f'"{executable or sys.executable}" -m pip install -U webbee'


def _notice_for(current: str, latest: "str | None", hint: str = "") -> str:
    new, cur = _ver(latest or ""), _ver(current)
    if new and cur and new > cur:           # never a notice from a version it cannot read
        return f"\U0001f41d webbee v{latest} available \u2014 upgrade: {hint or upgrade_hint()}"
    return ""


def check_update_status(current: str, *, cache_path, now: float, fetch,
                        ttl: float = 86400.0) -> "tuple[str, bool]":
    """(notice, checked) -- the freshness check WITH its own verdict (0.3.36).

    `check_for_update` collapses two very different outcomes into None: "you
    are on the newest release" and "we could not reach PyPI". Home's version
    badge must not claim "up to date" in the second case, so this variant also
    reports WHETHER the check actually resolved a latest version:
      * ("...v0.4.0 available...", True) -- a newer release exists
      * ("", True)  -- checked, nothing newer (safe to show "up to date")
      * ("", False) -- unknown (offline/first run): show the version only

    Never raises.
    """
    latest = _resolve_latest(current, cache_path=cache_path, now=now, fetch=fetch, ttl=ttl)
    return (_notice_for(current, latest), bool(latest))


def check_for_update(current: str, *, cache_path, now: float, fetch, ttl: float = 86400.0) -> "str | None":
    """Return a one-line upgrade notice if a newer webbee is on PyPI, else None.
    Caches the latest-seen version for `ttl` seconds. Never raises.
    Kept as-is (boot's banner path); `check_update_status` adds the verdict."""
    return _notice_for(current, _resolve_latest(
        current, cache_path=cache_path, now=now, fetch=fetch, ttl=ttl)) or None


def installed_version(python: str = "", run=None) -> str:
    """The webbee version on disk now IN THE PLACE THIS PROCESS RUNS FROM: its own ``webbee/__init__.py`` read again
    (an upgrade by pipx, uv or pip rewrites that file). Another interpreter's (``python``): read by it. "" when it cannot
    be read. Never raises.

    Live 2026-11-14 (Windows): the package metadata named a left-over older webbee (a user site-packages
    ``dist-info`` ahead of the real install on ``sys.path``), and the toolbar said "v... is installed, restart webbee"
    to someone already running the newest -- for good."""
    import subprocess
    import sys
    if not python:
        try:
            import re
            import webbee
            text = Path(webbee.__file__).read_text(encoding="utf-8")
            m = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
            if m:
                return m.group(1)
        except Exception:
            pass
    run = run or subprocess.run
    try:
        got = run([python or sys.executable, "-c", "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
                   "import importlib.metadata as m; print(m.version('webbee'))"],
                  capture_output=True, text=True, timeout=20)
        return str(got.stdout or "").strip() if got.returncode == 0 else ""
    except Exception:
        return ""


def restart_notice(running: str, installed: str) -> str:
    """Field report 06.11 #3: a pipx upgrade during a session left the old process running, and `webbee --version`
    (read from disk) said the new one. Said in the toolbar until the session restarts."""
    if installed and _ver(installed) and _ver(running) and _ver(installed) != _ver(running):
        return (f"\U0001f41d webbee v{installed} is installed, this session still runs v{running} "
                "\u2014 restart webbee to use it")
    return ""
