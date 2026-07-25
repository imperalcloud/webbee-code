import json
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/webbee/json"


def _ver(s: str) -> tuple:
    try:
        return tuple(int(x) for x in s.strip().split("."))
    except (ValueError, AttributeError):
        return ()


def default_fetch() -> "str | None":
    """Fetch the latest version from PyPI. Returns None on ANY failure
    (offline, timeout, parse) — the caller treats None as 'no update'."""
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


def _notice_for(current: str, latest: "str | None") -> str:
    if latest and _ver(latest) > _ver(current):
        return f"\U0001f41d webbee v{latest} available \u2014 upgrade: pipx upgrade webbee  (or: uv tool upgrade webbee)"
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
