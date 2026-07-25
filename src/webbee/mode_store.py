"""Mode persistence per-repo (T6.1, coding-remote flow perfection): remembers
the coding mode across process restarts by writing a tiny marker file under
``~/.cache/webbee/mode-{repo_key}`` -- the SAME repo identity (webbee.repo.
compute_repo_key) intel/checkpoints already key their own per-repo caches by.
Plain text, ONE mode string per file: no JSON, no schema to version.

Fail-soft in BOTH directions, by design:
  * `load_mode` -- a missing file, an unreadable dir, or corrupt/garbage
    content all degrade to None. The caller's own process-baseline mode is
    always the fallback (repl.run_repl's `mode` argument), so a bad cache is
    exactly as safe as no cache at all.
  * `save_mode` -- a write failure (read-only home, disk full, no
    permission) is silently dropped: losing the memory is a far smaller
    problem than crashing the terminal over a nice-to-have.

SECURITY POSTURE (Valentin-chosen, matches the terminal-local autopilot
confirm ladder in repl._confirm_autopilot): autopilot is NEVER remembered.
`save_mode` downgrades an autopilot write to 'default' before it ever
touches disk -- autopilot auto-approves every tool call, so upgrading to it
must be re-confirmed EXPLICITLY every process, never silently resumed from a
stale file the next time this repo is opened."""
from __future__ import annotations

import os

from webbee.repo import compute_repo_key, find_repo_root

_CACHE_DIR = os.path.expanduser("~/.cache/webbee")   # test seam: monkeypatch this name


# repo_key is derived from `git remote get-url origin` (compute_repo_key,
# timeout=5) -- measured ~11ms per call on a warm local repo, worst case the
# full 5s on a repo whose git call hangs. It is also INVARIANT for the life of
# the process: a workspace's git remote does not change under a running dock.
# Every OTHER caller already keeps that subprocess off the event loop via
# asyncio.to_thread (repl.py's boot paths), but save_mode is reached
# SYNCHRONOUSLY from the Shift-TAB key binding (tui `_cycle` ->
# repl.set_slot_mode -> save_mode), i.e. straight on the event loop, where it
# froze the dock on every single mode switch.
#
# Memoising per workspace fixes that for every switch after the first: the
# repeat cost is a dict lookup. The first call in a process still pays, which
# is acceptable because boot's own load_mode already warms this cache off-loop
# before any key can be pressed.
_KEY_CACHE: dict[str, str] = {}


def _repo_key_for(workspace: str) -> str:
    key = _KEY_CACHE.get(workspace)
    if key is None:
        key = compute_repo_key(find_repo_root(workspace))
        _KEY_CACHE[workspace] = key
    return key


def _path_for(workspace: str) -> str:
    return os.path.join(_CACHE_DIR, f"mode-{_repo_key_for(workspace)}")


def load_mode(workspace: str) -> "str | None":
    """The remembered mode for `workspace`'s repo, or None on no file / ANY
    error (corrupt content, permission denied, missing dir, git failure
    inside compute_repo_key) -- never raises."""
    try:
        with open(_path_for(workspace), "r", encoding="utf-8") as f:
            mode = f.read().strip()
        return mode or None
    except Exception:
        return None


def save_mode(workspace: str, mode: str) -> None:
    """Remember `mode` for `workspace`'s repo -- except autopilot, which is
    downgraded to 'default' before it's written (see module docstring).
    Never raises: a write failure just means the next boot won't remember,
    no worse than before this feature existed."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        stored = mode if mode != "autopilot" else "default"
        with open(_path_for(workspace), "w", encoding="utf-8") as f:
            f.write(stored)
    except Exception:
        pass
