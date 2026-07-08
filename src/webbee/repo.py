"""Repo identity (CORTEX U0, spec §2.2): repo_key = 12-hex sha256 of the git
remote URL (origin) or, for remoteless/non-git dirs, the repo-root realpath.
The root is found by walking UP to the nearest .git so a subdirectory launch
maps to the SAME key (fragmented memory otherwise). `.git` is a FILE (not a
dir) in a linked worktree or submodule -- os.path.exists (not isdir) so
those resolve to the correct root too."""
import hashlib
import os
import subprocess


def find_repo_root(start: str) -> str:
    cur = os.path.realpath(start)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.realpath(start)
        cur = parent


def compute_repo_key(root: str) -> str:
    ident = ""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            ident = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        ident = ""
    ident = ident or os.path.realpath(root)
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
