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


def check_for_update(current: str, *, cache_path, now: float, fetch, ttl: float = 86400.0) -> "str | None":
    """Return a one-line upgrade notice if a newer webbee is on PyPI, else None.
    Caches the latest-seen version for `ttl` seconds. Never raises."""
    cache_path = Path(cache_path)
    latest = None
    try:
        cached = json.loads(cache_path.read_text())
        if now - float(cached.get("checked_at", 0)) < ttl:
            latest = cached.get("latest")
    except Exception:
        latest = None

    if latest is None:
        latest = fetch()
        if latest:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"latest": latest, "checked_at": now}))
            except Exception:
                pass

    if latest and _ver(latest) > _ver(current):
        return f"🐝 webbee v{latest} доступна — обнови: pipx upgrade webbee  (или: uv tool upgrade webbee)"
    return None
