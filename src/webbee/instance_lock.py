"""Per-repo single-instance advisory lock (0.3.25 Part C, second-instance
collision fix): today a second `webbee` launched in the SAME repo derives
the SAME legacy session id as the first (tab-1's `slot_id` is always ""),
so both processes poll/stream the IDENTICAL gateway session and starve each
other -- looks frozen, live-reproduced twice (Valentin, 2026-07-19/20).

`acquire(repo_key)` takes a non-blocking exclusive `flock` on
`~/.cache/webbee/instance-{repo_key}.lock` (same repo-identity/cache-dir
house pattern as `mode_store` — `webbee.repo.compute_repo_key`). The FIRST
process to open a given repo keeps the fd open for its whole lifetime; the
kernel drops the flock automatically the instant that fd closes — process
exit, crash, `kill -9`, all release it for free, no stale-lock cleanup ever
needed anywhere. A SECOND process trying the SAME repo_key finds the lock
already held and gets `held=True` back — `repl._make_session_slot` reads
that to mint a fresh `slot_id` for what would otherwise be its own tab 1,
exactly like every LATER tab already does, and leaves an honest transcript
note instead of silently starving.

Fail-soft in BOTH directions, matching `mode_store`'s posture: ANY error
acquiring the lock (a read-only home, a full disk, no `fcntl` on this
platform, a filesystem that doesn't support `flock` at all) degrades to
"proceed as primary" (`held=False`) — a false negative here is, at worst,
the very starvation this module exists to fix; it is never a reason to
crash the terminal."""
from __future__ import annotations

import os

_CACHE_DIR = os.path.expanduser("~/.cache/webbee")   # test seam: monkeypatch this name


def _path_for(repo_key: str) -> str:
    return os.path.join(_CACHE_DIR, f"instance-{repo_key}.lock")


class InstanceLock:
    """The result of one `acquire()` call. `held` is True iff SOME OTHER
    process already owned this repo's lock at that moment (this instance is
    SECONDARY); `close()` releases the fd (a no-op the second time, and on a
    `held=True` result — there is nothing of this instance's own to
    release) and never raises."""

    def __init__(self, held: bool, fd: "int | None") -> None:
        self.held = held
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def acquire(repo_key: str) -> InstanceLock:
    """Try to become the primary Webbee for `repo_key`. Never raises: any
    failure along the way degrades to `InstanceLock(held=False, fd=None)`
    (module docstring) — this instance simply proceeds as primary."""
    try:
        import fcntl
        os.makedirs(_CACHE_DIR, exist_ok=True)
        fd = os.open(_path_for(repo_key), os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:
        # No fcntl at all (a non-POSIX platform), an unwritable/missing
        # cache dir, or any other setup failure -- there is no fd to clean
        # up, proceed as primary.
        return InstanceLock(held=False, fd=None)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # The expected "someone else already holds it" outcome
        # (EWOULDBLOCK/EAGAIN, surfaced as BlockingIOError -- an OSError
        # subclass) -- this instance is SECONDARY.
        os.close(fd)
        return InstanceLock(held=True, fd=None)
    except Exception:
        # Any OTHER unexpected failure locking a perfectly good fd -- same
        # fail-soft posture, but this one really is ours to close since
        # flock never actually succeeded on it either way.
        os.close(fd)
        return InstanceLock(held=False, fd=None)
    return InstanceLock(held=False, fd=fd)
