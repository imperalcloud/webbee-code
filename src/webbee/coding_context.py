"""Coding-context snapshot for the cloud brain (extracted verbatim from
webbee.session): build_coding_context packages cwd/git/tree/repo identity
(+ optional repo_profile), and detect_verify_cmd is the CLIENT-detected
proof-of-done command a marathon carries in that context."""
import os
import subprocess

# Heavy dependency/build dirs the file-tree walk and the agent `grep` tool
# must NEVER descend: they blow up walk time on real repos and (with the tree's
# 200-file cap) would otherwise fill the snapshot with dependency junk instead
# of the user's own code. `.git` + all dotdirs are pruned separately by the
# callers; these are the non-dot offenders. Shared with tools._t_grep.
WALK_IGNORE_DIRS = frozenset({
    "node_modules", "vendor", "dist", "build", "target", "__pycache__",
    ".git", ".venv", "venv", ".next", ".cache", ".tox", ".mypy_cache",
    ".pytest_cache",
})


def build_coding_context(workspace_root: str, intel=None) -> dict:
    """Snapshot handed to the cloud brain: cwd (realpath), `git status -sb`
    (empty for non-git/any error), a bounded newline-joined file tree, and —
    when a ready intel service is injected — the precomputed repo_profile.
    The profile is READ from the already-built index (cheap); indexing never
    happens inline here."""
    cwd = os.path.realpath(workspace_root)
    try:
        proc = subprocess.run(
            ["git", "status", "-sb"], cwd=cwd,
            capture_output=True, text=True, timeout=10,
        )
        git = proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        git = ""
    paths = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames
                       if d not in WALK_IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            paths.append(os.path.relpath(os.path.join(dirpath, fn), cwd))
            if len(paths) >= 200:
                break
        if len(paths) >= 200:
            break
    from webbee.repo import compute_repo_key, find_repo_root
    root = find_repo_root(cwd)
    d = {"cwd": cwd, "git": git, "tree": "\n".join(paths),
         "repo_key": compute_repo_key(root), "repo_root": root}
    if intel is not None and getattr(intel, "ready", False):
        try:
            d["repo_profile"] = intel.repo_profile()
        except Exception:
            pass
    return d


def detect_verify_cmd(repo_root: str) -> str:
    """Best-effort project test command, CLIENT-detected from the repo layout.

    SECURITY-load-bearing: in a marathon the kernel runs ONLY this command as
    proof-of-done — the cloud brain can never author a shell command. So this
    is deliberately small + honest: a fixed command per recognised ecosystem,
    NO guessing beyond these. An empty string means "no known runner" → the
    kernel falls back to an LLM-judged done-check. Checked in a stable order."""
    import json as _json

    root = repo_root or "."

    def _has(name: str) -> bool:
        return os.path.isfile(os.path.join(root, name))

    if _has("pyproject.toml") or _has("setup.cfg") or _has("tox.ini"):
        return "pytest -q"
    if _has("package.json"):
        try:
            with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
                pkg = _json.load(f)
            scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
            if isinstance(scripts, dict) and scripts.get("test"):
                return "npm test"
        except (OSError, ValueError):
            pass
    if _has("Cargo.toml"):
        return "cargo test"
    if _has("go.mod"):
        return "go test ./..."
    if _has("Makefile"):
        try:
            import re as _re
            with open(os.path.join(root, "Makefile"), encoding="utf-8") as f:
                if _re.search(r"(?m)^test:", f.read()):
                    return "make test"
        except OSError:
            pass
    return ""
