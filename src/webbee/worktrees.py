"""Auto-worktree isolation for same-repo second tabs (W4b T5). Opening a
SECOND session tab against a repo root some OTHER slot already has open
gives that tab its own `git worktree` checkout — parallel writes from two
tabs in the same repo no longer collide (the "my drill leaked into your
thread" class of bug). Every call here is synchronous (plain `subprocess`) —
callers run it via `asyncio.to_thread` so a slow/hanging git never blocks the
event loop.

Fail-soft by design: ANY failure (no git binary, not a git repo, a dirty ref,
disk full, timeout) returns None/False rather than raising — the caller
degrades to the SHARED checkout with an honest note, never blocking the tab
from opening at all."""
import os
import subprocess

WORKTREE_ROOT = os.path.expanduser("~/.cache/webbee/worktrees")
_ADD_TIMEOUT_S = 30
_STATUS_TIMEOUT_S = 10


def worktree_path(repo_root: str, slot_id: str) -> str:
    """PURE — the deterministic path a given (repo_root, slot_id) pair would
    live at, `{basename}-{slot_id}` under the shared cache root. Two repos
    with the same basename can't collide in practice: slot_id is minted
    fresh per tab (uuid4 hex), so the pair is unique even then."""
    basename = os.path.basename(os.path.normpath(repo_root)) or "repo"
    return os.path.join(WORKTREE_ROOT, f"{basename}-{slot_id}")


def create_worktree(repo_root: str, slot_id: str) -> "str | None":
    """`git worktree add <path> HEAD` off `repo_root` — returns the new
    worktree's path on success, None on ANY failure (missing git, repo_root
    not a git repo, a path collision, timeout). Never raises."""
    path = worktree_path(repo_root, slot_id)
    try:
        os.makedirs(WORKTREE_ROOT, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", repo_root, "worktree", "add", path, "HEAD"],
            capture_output=True, text=True, timeout=_ADD_TIMEOUT_S,
        )
        if proc.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return path


def is_clean(path: str) -> bool:
    """True iff `git status --porcelain` at `path` reports no changes at
    all. Used by slot-close (v1: informational only — a worktree is NEVER
    auto-removed, clean or not; see the module docstring's safety note)."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=_STATUS_TIMEOUT_S,
        )
        return proc.returncode == 0 and not proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False


def list_worktrees() -> list:
    """Every worktree directory ever created under the cache root (this
    process or a past one) — a plain filesystem listing, no git calls, so it
    never blocks or fails on a broken checkout. [] when the cache root
    itself doesn't exist yet (no worktree has ever been created)."""
    if not os.path.isdir(WORKTREE_ROOT):
        return []
    return sorted(
        os.path.join(WORKTREE_ROOT, name)
        for name in os.listdir(WORKTREE_ROOT)
        if os.path.isdir(os.path.join(WORKTREE_ROOT, name))
    )
