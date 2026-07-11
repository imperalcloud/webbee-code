"""The session's TIME MACHINE (whole-mind Phase 4, I-WEBBEE-CODE-REVERSIBLE).

A shadow git repository whose GIT_DIR lives in the webbee cache and whose
worktree is the user's workspace (the classic detached-git-dir pattern).
The user's own VCS -- .git, index, refs, hooks -- is NEVER touched:
reversibility is ours, not theirs. Every write tool auto-checkpoints before
it runs (tools.py); `rollback` snapshots the pre-rollback state first, so a
rollback is itself undoable. Fail-soft everywhere: no git binary / init
failure => available=False and every operation degrades to an honest
message -- the time machine must never block the work."""
import os
import subprocess

# Identity + safety for shadow commits only (never the user's config/hooks).
_GIT_CFG = ["-c", "user.name=webbee", "-c", "user.email=webbee@imperal.io",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


def shadow_key(workspace_root: str) -> str:
    """Shadow identity is the WORKTREE, never the remote: clones and linked
    git-worktrees of one origin must each get their OWN time machine
    (final-review F3 -- a shared shadow rolls checkout B onto checkout A's
    snapshot). sha256 of the realpath, 12 hex chars (intel keeps its own
    content-addressed repo_key; this one is deliberately different)."""
    import hashlib
    real = os.path.realpath(workspace_root)
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:12]


def _scrubbed_env() -> dict:
    """A GIT_*-free copy of the environment (final-review F2): git exports
    GIT_INDEX_FILE / GIT_DIR / GIT_OBJECT_DIRECTORY / GIT_COMMON_DIR into hook
    and rebase contexts -- inherited, they silently redirect SHADOW operations
    into the USER's repository. Explicit --git-dir/--work-tree flags override
    only two of them; scrub them all."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class ShadowGit:
    _DIFF_CAP = 20000

    def __init__(self, workspace_root: str, repo_key: str, cache_dir: str = "") -> None:
        self.root = os.path.realpath(workspace_root)
        base = cache_dir or os.path.expanduser("~/.cache/webbee")
        self.git_dir = os.path.join(base, "shadow", repo_key)
        self.available = False
        self.auto_ok = True        # auto-checkpointing enabled (F8 latch below)
        self._auto_fail_streak = 0  # consecutive failed AUTO snapshots

    def _git(self, *args: str) -> "subprocess.CompletedProcess":
        cmd = (["git", "--git-dir", self.git_dir, "--work-tree", self.root]
               + _GIT_CFG + list(args))
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              env=_scrubbed_env())

    def ensure(self) -> bool:
        """Create/open the shadow. Returns availability; never raises.
        Refuses a workspace that IS a bare git repository (final-review F9:
        tracking its refs/objects and resetting them would rewind the USER's
        VCS -- the one thing the shadow must never do)."""
        try:
            if (os.path.exists(os.path.join(self.root, "HEAD"))
                    and os.path.isdir(os.path.join(self.root, "objects"))
                    and os.path.isdir(os.path.join(self.root, "refs"))):
                self.available = False
                return False
            os.makedirs(self.git_dir, mode=0o700, exist_ok=True)
            os.chmod(self.git_dir, 0o700)
            if not os.path.exists(os.path.join(self.git_dir, "HEAD")):
                r = subprocess.run(["git"] + _GIT_CFG + ["init", "--bare", self.git_dir],
                                   capture_output=True, text=True, timeout=60,
                                   env=_scrubbed_env())
                if r.returncode != 0:
                    self.available = False
                    return False
                # Detached-git-dir mode: bare layout + explicit --work-tree.
                self._git("config", "core.bare", "false")
                excl = os.path.join(self.git_dir, "info", "exclude")
                os.makedirs(os.path.dirname(excl), exist_ok=True)
                with open(excl, "a", encoding="utf-8") as f:
                    f.write("\n.git/\n")
            self.available = self._git("rev-parse", "--git-dir").returncode == 0
        except Exception:
            self.available = False
        return self.available

    # -- internals ---------------------------------------------------------

    def _count(self) -> int:
        r = self._git("for-each-ref", "--format=%(refname)", "refs/webbee/")
        return len([ln for ln in r.stdout.splitlines() if ln.strip()])

    def _resolve(self, ref: str) -> "str | None":
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.isdigit():
            cand = f"refs/webbee/cp-{ref}"
        elif ref.startswith("cp-"):
            cand = f"refs/webbee/{ref}"
        else:
            cand = ref
        r = self._git("rev-parse", "--verify", "--quiet", cand + "^{commit}")
        return r.stdout.strip() if r.returncode == 0 else None

    # -- operations ---------------------------------------------------------

    def checkpoint(self, label: str = "", force: bool = False) -> "dict | None":
        """Commit the workspace state to the shadow history.
        {"id","n","label","changed"} or None when unavailable/failed.
        `force=True` always records a new checkpoint even with nothing dirty
        -- used by rollback() for its mandatory pre-rollback safety snapshot,
        which must exist even when the workspace already matches HEAD."""
        if not self.available:
            return None
        try:
            if self._git("add", "-A").returncode != 0:
                return None          # a partial add must never masquerade as a snapshot
            dirty = self._git("status", "--porcelain").stdout.strip()
            head = self._git("rev-parse", "--short", "HEAD")
            if not dirty and head.returncode == 0 and not force:
                return {"id": head.stdout.strip(), "n": self._count(),
                        "label": "(no changes)", "changed": False}
            msg = (label or "checkpoint").strip()[:200] or "checkpoint"
            args = ["commit", "-m", msg, "--no-verify"]
            if head.returncode != 0 or not dirty:
                args.append("--allow-empty")      # first-ever baseline / forced snapshot
            c = self._git(*args)
            if c.returncode != 0:
                return None
            sha = self._git("rev-parse", "--short", "HEAD").stdout.strip()
            n = self._count() + 1
            self._git("update-ref", f"refs/webbee/cp-{n}", "HEAD")
            return {"id": sha, "n": n, "label": msg, "changed": True}
        except Exception:
            return None

    def list_checkpoints(self, limit: int = 10) -> list:
        """Newest-first [{'n','id','label','when'}] from the refs/webbee ledger."""
        if not self.available:
            return []
        try:
            r = self._git(
                "for-each-ref",
                "--format=%(refname:short)|%(objectname:short)|%(contents:subject)|%(creatordate:relative)",
                "refs/webbee/")
            rows = []
            for line in r.stdout.splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4 and parts[0].startswith("webbee/cp-"):
                    rows.append({"n": int(parts[0].rsplit("-", 1)[1]),
                                 "id": parts[1], "label": parts[2], "when": parts[3]})
            rows.sort(key=lambda x: x["n"], reverse=True)
            return rows[:max(1, int(limit))]
        except Exception:
            return []

    def diff(self, since: str = "") -> str:
        """Workspace changes since a checkpoint (default: the last one).
        Bounded with an HONEST truncation note -- never a silent cut."""
        if not self.available:
            return "reversibility is off (git unavailable)"
        try:
            self._git("add", "-A")   # stage into the SHADOW index so new files show
            target = self._resolve(since) or "HEAD"
            stat = self._git("diff", "--cached", "--stat", target).stdout
            body = self._git("diff", "--cached", target).stdout
            out = (stat + "\n" + body).strip() or "(no changes)"
            if len(out) > self._DIFF_CAP:
                out = (out[:self._DIFF_CAP]
                       + f"\n… (diff truncated at {self._DIFF_CAP} chars -- pass a "
                         f"narrower `since` or inspect files directly)")
            return out
        except Exception as e:
            return f"diff failed: {type(e).__name__}: {e}"

    def rollback(self, to: str) -> dict:
        """Restore every checkpointed file to checkpoint `to`. The current
        state is checkpointed FIRST ("pre-rollback"), so a rollback is itself
        undoable. Files never checkpointed are not removed."""
        if not self.available:
            return {"ok": False, "content": "reversibility is off (git unavailable)"}
        target = self._resolve(to)
        if not target:
            return {"ok": False, "content":
                    f"unknown checkpoint '{to}' -- use an id, cp-N or N "
                    f"(see the checkpoint list)"}
        undo = self.checkpoint("pre-rollback", force=True)
        if not undo:
            # The safety snapshot is MANDATORY: without it a reset would
            # destroy the current state unrecoverably (final-review F1).
            return {"ok": False, "content":
                    "rollback refused: the pre-rollback safety snapshot could not "
                    "be created (shadow git error), so rolling back would destroy "
                    "the current state unrecoverably. Fix the shadow (disk space / "
                    "permissions) or copy your changes out first."}
        r = self._git("reset", "--hard", target)
        if r.returncode != 0:
            return {"ok": False, "content": f"rollback failed: {r.stderr.strip()[:500]}"}
        return {"ok": True, "content":
                (f"restored the workspace to checkpoint {to} ({target[:9]}). "
                 f"The previous state -- including any files this rollback removed -- "
                 f"was saved first as checkpoint cp-{(undo or {}).get('n', '?')} "
                 f"({(undo or {}).get('id', '?')}), so this rollback is itself undoable.")}

    _AUTO_FAIL_LATCH = 3   # consecutive AUTO failures before pausing auto-checkpointing

    def note_auto_result(self, ok: bool) -> None:
        """Record an AUTO (pre-write) snapshot outcome. A single transient
        failure must NOT disable the session's time machine (final-review F8):
        latch auto-checkpointing OFF only after _AUTO_FAIL_LATCH CONSECUTIVE
        failures; any success resets the streak and re-enables it."""
        if ok:
            self._auto_fail_streak = 0
            self.auto_ok = True
        else:
            self._auto_fail_streak += 1
            if self._auto_fail_streak >= self._AUTO_FAIL_LATCH:
                self.auto_ok = False

    def describe(self) -> str:
        """One printable block for the /checkpoints command."""
        if not self.available:
            return "Reversibility is off (git unavailable)."
        rows = self.list_checkpoints(limit=10)
        if not rows:
            body = "No checkpoints yet."
        else:
            lines = [f"cp-{r['n']}  {r['id']}  {r['when']:>16}  {r['label']}" for r in rows]
            body = "Checkpoints (newest first) — /rollback <id|cp-N|N>:\n" + "\n".join(lines)
        if not self.auto_ok:
            body = ("⚠ Auto-checkpointing is paused after repeated shadow errors — "
                     "manual /checkpoint + /rollback still work.\n") + body
        return body
