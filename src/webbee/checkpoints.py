"""The session's TIME MACHINE (whole-mind Phase 4, I-WEBBEE-CODE-REVERSIBLE).

A shadow git repository whose GIT_DIR lives in the webbee cache and whose
worktree is the user's workspace (the classic detached-git-dir pattern).
The user's own VCS -- .git, index, refs, hooks -- is NEVER touched:
reversibility is ours, not theirs. Every write tool auto-checkpoints before
it runs (tools.py); `rollback` snapshots the pre-rollback state first, so a
rollback is itself undoable. Fail-soft everywhere: no git binary / init
failure => available=False and every operation degrades to an honest
message -- the time machine must never block the work.

Nested repositories (code review C3, 2026-10-30): a workspace that is a
folder of repositories (wpanel/core/gateway/.git, wpanel/modules/x/.git) or a
repo holding nested repos/submodules. `git add -A` stores each nested repo as
a gitlink (mode 160000) with none of its files, so a rollback restored none
of the agent's edits there while saying it restored the workspace. The
snapshot is now built file by file: each nested repo's files are listed by
its own git (read-only `ls-files`, its .gitignore honored) and staged into
the SHADOW index as plain blobs. Dependency/cache folders and files over
_MAX_FILE_BYTES are left out, and every snapshot and rollback names what it
did not cover."""
import os
import subprocess
import time

# Identity + safety for shadow commits only (never the user's config/hooks).
_GIT_CFG = ["-c", "user.name=webbee", "-c", "user.email=webbee@imperal.io",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]

# Folders never snapshotted: dependencies, virtualenvs, caches and any .git.
# Copying node_modules / a venv before every write made each bash call block
# up to its timeout on large trees until auto-checkpointing switched itself off.
_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv",
                        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"})
_EXCLUDE_LINES = [d + "/" for d in sorted(_SKIP_DIRS)]
_SKIP_RULE = ("dependency and cache folders (node_modules, __pycache__, virtualenvs, "
              ".venv, tool caches) and files a .gitignore excludes are never snapshotted")
_MAX_FILE_BYTES = 20 * 1024 * 1024   # a larger file is left out and named
_SNAPSHOT_BUDGET_S = 30.0            # one snapshot never holds a tool call longer
_CHUNK = 2000                        # paths per update-index call: progress is kept on a timeout
_NAMED_CAP = 50                      # uncovered paths named per snapshot / result


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


def _is_repo_dir(path: str) -> bool:
    """A directory that is its own git work tree (.git dir, or a .git file of a submodule/linked worktree)."""
    return (not os.path.islink(path) and os.path.isdir(path)
            and os.path.lexists(os.path.join(path, ".git")))


def _split_z(out: str) -> list:
    return [p for p in out.split("\0") if p]


class _Budget(Exception):
    """The snapshot ran past _SNAPSHOT_BUDGET_S."""


class ShadowGit:
    _DIFF_CAP = 20000

    def __init__(self, workspace_root: str, repo_key: str, cache_dir: str = "") -> None:
        self.root = os.path.realpath(workspace_root)
        base = cache_dir or os.path.expanduser("~/.cache/webbee")
        self.git_dir = os.path.join(base, "shadow", repo_key)
        self.available = False
        self.auto_ok = True        # auto-checkpointing enabled (F8 latch below)
        self._auto_fail_streak = 0  # consecutive failed AUTO snapshots
        self.pause_reason = ""      # why auto-checkpointing stopped at once (a too-slow snapshot)

    def _git(self, *args: str) -> "subprocess.CompletedProcess":
        return self._git_io(list(args))

    def _git_io(self, args: list, stdin: str = "", deadline: float = 0.0,
                index_file: str = "") -> "subprocess.CompletedProcess":
        """One SHADOW git call; `deadline` (time.monotonic) bounds it, `index_file` swaps the shadow index."""
        cmd = (["git", "--git-dir", self.git_dir, "--work-tree", self.root]
               + _GIT_CFG + list(args))
        env = _scrubbed_env()
        if index_file:
            env["GIT_INDEX_FILE"] = index_file
        return subprocess.run(cmd, input=stdin if stdin else None, capture_output=True, text=True,
                              timeout=self._left(deadline), env=env)

    @staticmethod
    def _left(deadline: float) -> float:
        if not deadline:
            return 120
        left = deadline - time.monotonic()
        if left <= 0:
            raise _Budget()
        return left

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
            # Rewritten on every open, so a shadow made before the excludes gets them too.
            excl = os.path.join(self.git_dir, "info", "exclude")
            os.makedirs(os.path.dirname(excl), exist_ok=True)
            with open(excl, "w", encoding="utf-8") as f:
                f.write("# written by webbee: never snapshotted\n" + "\n".join(_EXCLUDE_LINES) + "\n")
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

    def _nested_ls(self, rel: str, deadline: float) -> "tuple[list, str]":
        """The files a nested repository's own git would show (tracked + untracked, its ignores honored),
        relative to that repo. Read-only: `ls-files` never writes the user's index; optional locks off."""
        cmd = (["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                "-C", os.path.join(self.root, rel), "ls-files", "-z", "-c", "-o", "--exclude-standard"]
               + [f"--exclude={x}" for x in _EXCLUDE_LINES])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=self._left(deadline),
                           env=_scrubbed_env())
        if r.returncode != 0:
            return [], (r.stderr.strip().splitlines() or ["git ls-files failed"])[0][:200]
        return _split_z(r.stdout), ""

    def _collect(self, deadline: float) -> "tuple[list, list, list]":
        """(files, nested_repos, uncovered) -- every workspace file to snapshot, the nested repositories
        whose files are among them, and [(path, why)] for what is left out by name."""
        listed = _split_z(self._git_io(["ls-files", "-z", "-c", "-o", "--exclude-standard"],
                                       deadline=deadline).stdout)
        repo_cache: dict = {}

        def is_repo(rel: str) -> bool:
            if rel not in repo_cache:
                repo_cache[rel] = _is_repo_dir(os.path.join(self.root, rel))
            return repo_cache[rel]

        files, roots, uncovered = set(), set(), []
        for p in listed:
            if p.endswith("/"):                      # git shows an untracked nested repo as "dir/"
                if is_repo(p[:-1]):
                    roots.add(p[:-1])
                continue
            parts = p.split("/")
            owner = next(("/".join(parts[:i]) for i in range(1, len(parts)) if is_repo("/".join(parts[:i]))), "")
            if owner:                                # inside a nested repo: its own git lists it below
                roots.add(owner)
            elif is_repo(p):                         # a gitlink an older snapshot stored
                roots.add(p)
            else:
                files.add(p)
        queue, nested = sorted(roots), []
        while queue:
            rel = queue.pop(0)
            if any(seg in _SKIP_DIRS for seg in rel.split("/")):
                continue
            entries, err = self._nested_ls(rel, deadline)
            if err:
                uncovered.append((rel + "/", f"nested repository its git could not list ({err})"))
                continue
            nested.append(rel)
            for e in entries:
                full = rel + "/" + e.rstrip("/")
                fp = os.path.join(self.root, full)
                if e.endswith("/") or (not os.path.islink(fp) and os.path.isdir(fp)):
                    if is_repo(full):                # a repo inside the nested repo, any depth
                        queue.append(full)
                    continue
                files.add(full)
        venvs = {os.path.dirname(p) for p in files if os.path.basename(p) == "pyvenv.cfg"} - {""}
        keep = []
        for p in sorted(files):
            parts = p.split("/")
            if any(seg in _SKIP_DIRS for seg in parts[:-1]):
                continue
            if any(p.startswith(v + "/") for v in venvs):
                continue
            try:
                st = os.lstat(os.path.join(self.root, p))
            except OSError:
                continue                             # gone: dropped from the snapshot below
            if st.st_size > _MAX_FILE_BYTES and not os.path.islink(os.path.join(self.root, p)):
                uncovered.append((p, f"larger than {_MAX_FILE_BYTES // (1024 * 1024)} MB"))
                continue
            keep.append(p)
        for v in sorted(venvs):
            uncovered.append((v + "/", "a Python virtualenv (pyvenv.cfg)"))
        return keep, nested, uncovered

    def _stage(self, deadline: float) -> "tuple[bool, list, list]":
        """Make the SHADOW index match the workspace: (ok, nested_repos, uncovered). Paths are staged as
        plain files, so a nested repository's edits become blobs, never a gitlink."""
        keep, nested, uncovered = self._collect(deadline)
        r = self._git_io(["ls-files", "-z", "-c"], deadline=deadline)
        if r.returncode != 0:
            return False, nested, uncovered
        gone = sorted(set(_split_z(r.stdout)) - set(keep))
        for i in range(0, len(gone), _CHUNK):        # first: a gitlink must leave before its files come in
            if self._git_io(["update-index", "-z", "--force-remove", "--stdin"],
                            stdin="\0".join(gone[i:i + _CHUNK]) + "\0", deadline=deadline).returncode != 0:
                return False, nested, uncovered
        for i in range(0, len(keep), _CHUNK):
            if self._git_io(["update-index", "-z", "--add", "--remove", "--stdin"],
                            stdin="\0".join(keep[i:i + _CHUNK]) + "\0", deadline=deadline).returncode != 0:
                return False, nested, uncovered
        return True, nested, uncovered

    def _drop_stale_lock(self) -> None:
        """A git call killed at the budget can leave the SHADOW index.lock behind; it is ours to remove."""
        try:
            os.unlink(os.path.join(self.git_dir, "index.lock"))
        except OSError:
            pass

    @staticmethod
    def _body(nested: list, uncovered: list) -> str:
        lines = [f"nested repository: {n}" for n in nested[:_NAMED_CAP]]
        lines += [f"not snapshotted: {p} -- {why}" for p, why in uncovered[:_NAMED_CAP]]
        more = max(0, len(nested) - _NAMED_CAP) + max(0, len(uncovered) - _NAMED_CAP)
        if more:
            lines.append(f"not snapshotted: ... and {more} more")
        return "\n".join(lines)

    def _read_body(self, commit: str) -> "tuple[list, list]":
        """(nested repos, [(path, why)]) a checkpoint recorded about itself."""
        body = self._git("log", "-1", "--format=%b", commit).stdout
        nested, uncovered = [], []
        for ln in body.splitlines():
            if ln.startswith("nested repository: "):
                nested.append(ln[len("nested repository: "):])
            elif ln.startswith("not snapshotted: "):
                p, _, why = ln[len("not snapshotted: "):].partition(" -- ")
                uncovered.append((p, why))
        return nested, uncovered

    # -- operations ---------------------------------------------------------

    def checkpoint(self, label: str = "", force: bool = False) -> "dict | None":
        """Commit the workspace state to the shadow history.
        {"id","n","label","changed","paths","nested","uncovered"} or None when unavailable/failed --
        ``paths``: the workspace paths this snapshot changed (sorted; [] when none);
        ``nested``: nested repositories whose files the snapshot holds;
        ``uncovered``: [(path, why)] left out by name (too large, a virtualenv, an unlistable repo).
        `force=True` always records a new checkpoint even with nothing dirty
        -- used by rollback() for its mandatory pre-rollback safety snapshot,
        which must exist even when the workspace already matches HEAD."""
        if not self.available:
            return None
        try:
            deadline = time.monotonic() + _SNAPSHOT_BUDGET_S
            ok, nested, uncovered = self._stage(deadline)
            if not ok:
                return None          # a partial stage must never masquerade as a snapshot
            porcelain = self._git("status", "--porcelain", "--untracked-files=no").stdout
            dirty = porcelain.strip()
            # each porcelain line is "XY path": its first two columns are the status -- a strip of the whole output took
            # the first line's leading space and then its path's first letter (Webbee 2026-10-25: "mperal_kernel/...")
            paths = sorted({ln[3:].split(" -> ")[-1].strip().strip('"') for ln in porcelain.splitlines() if len(ln) > 3})
            head = self._git("rev-parse", "--short", "HEAD")
            if not dirty and head.returncode == 0 and not force:
                return {"id": head.stdout.strip(), "n": self._count(),
                        "label": "(no changes)", "changed": False, "paths": [],
                        "nested": nested, "uncovered": uncovered}
            msg = (label or "checkpoint").strip()[:200] or "checkpoint"
            args = ["commit", "-m", msg, "--no-verify"]
            body = self._body(nested, uncovered)
            if body:
                args[3:3] = ["-m", body]
            if head.returncode != 0 or not dirty:
                args.append("--allow-empty")      # first-ever baseline / forced snapshot
            c = self._git(*args)
            if c.returncode != 0:
                return None
            sha = self._git("rev-parse", "--short", "HEAD").stdout.strip()
            n = self._count() + 1
            self._git("update-ref", f"refs/webbee/cp-{n}", "HEAD")
            return {"id": sha, "n": n, "label": msg, "changed": True, "paths": paths,
                    "nested": nested, "uncovered": uncovered}
        except (_Budget, subprocess.TimeoutExpired):
            # Too large to snapshot within the budget: stop AUTO snapshots now instead of holding the next
            # tool calls for a full timeout each (C3); the staged part is kept, so a manual /checkpoint resumes.
            self._drop_stale_lock()
            self.auto_ok = False
            self.pause_reason = (f"a snapshot of this workspace took longer than {int(_SNAPSHOT_BUDGET_S)}s")
            return None
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
            note = ""
            try:                     # stage into the SHADOW index so new files show
                ok, _nested, _unc = self._stage(time.monotonic() + _SNAPSHOT_BUDGET_S)
                if not ok:
                    note = "\n(note: the workspace could not be fully staged -- this diff may be incomplete)"
            except (_Budget, subprocess.TimeoutExpired):
                self._drop_stale_lock()
                note = (f"\n(note: staging the workspace took longer than {int(_SNAPSHOT_BUDGET_S)}s "
                        f"-- this diff may be incomplete)")
            target = self._resolve(since) or "HEAD"
            stat = self._git("diff", "--cached", "--stat", target).stdout
            body = self._git("diff", "--cached", target).stdout
            out = (stat + "\n" + body).strip() or "(no changes)"
            if len(out) > self._DIFF_CAP:
                out = (out[:self._DIFF_CAP]
                       + f"\n… (diff truncated at {self._DIFF_CAP} chars -- pass a "
                         f"narrower `since` or inspect files directly)")
            return out + note
        except Exception as e:
            return f"diff failed: {type(e).__name__}: {e}"

    def _tree(self, commit: str) -> list:
        """[(mode, path)] of every entry of a commit's tree."""
        out = self._git("ls-tree", "-r", "-z", "--full-tree", commit).stdout
        rows = []
        for rec in _split_z(out):
            meta, _, path = rec.partition("\t")
            rows.append((meta.split(" ", 1)[0], path))
        return rows

    def _without_gitlinks(self, target: str, pre: str, gitlinks: list) -> "str | None":
        """A commit equal to `target` except that each gitlink path keeps the files the safety snapshot
        `pre` holds there -- a checkpoint taken before nested repos were snapshotted has no files for
        them, and checking its gitlink out would delete the nested repo's current work tree."""
        idx = os.path.join(self.git_dir, "webbee-rollback-index")
        try:
            if self._git_io(["read-tree", target], index_file=idx).returncode != 0:
                return None
            if self._git_io(["update-index", "-z", "--force-remove", "--stdin"],
                            stdin="\0".join(gitlinks) + "\0", index_file=idx).returncode != 0:
                return None
            keep = self._git_io(["ls-tree", "-r", "-z", "--full-tree", pre, "--"] + gitlinks).stdout
            if keep and self._git_io(["update-index", "-z", "--index-info"], stdin=keep,
                                     index_file=idx).returncode != 0:
                return None
            tree = self._git_io(["write-tree"], index_file=idx)
            if tree.returncode != 0:
                return None
            c = self._git("commit-tree", tree.stdout.strip(), "-p", target, "-m",
                          "rollback view: nested repositories kept as they are")
            return c.stdout.strip() if c.returncode == 0 else None
        finally:
            try:
                os.unlink(idx)
            except OSError:
                pass

    def rollback(self, to: str) -> dict:
        """Restore every checkpointed file to checkpoint `to`. The current
        state is checkpointed FIRST ("pre-rollback"), so a rollback is itself
        undoable. Files never checkpointed are not removed. A file the safety
        snapshot could not hold (too large, excluded) is never overwritten, and
        the result names every path the rollback did not restore, with why."""
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
        try:
            pre = self._git("rev-parse", "HEAD").stdout.strip()
            t_rows = self._tree(target)
            gitlinks = [p for mode, p in t_rows if mode == "160000"]
            goal = target
            if gitlinks:
                goal = self._without_gitlinks(target, pre, gitlinks)
                if not goal:
                    return {"ok": False, "content":
                            "rollback refused: checkpoint " + to + " holds nested repositories only as "
                            "pointers and the shadow could not build a view that keeps them; nothing was changed."}
            pre_paths = {p for _m, p in self._tree(pre)}
            goal_paths = {p for _m, p in self._tree(goal)}
            unsaved = dict(undo.get("uncovered") or [])
            held = []
            for p in sorted(goal_paths - pre_paths):
                full = os.path.join(self.root, p)
                if os.path.lexists(full) and (os.path.islink(full) or not os.path.isdir(full)):
                    held.append(p)
            moved = []
            try:
                for p in held:               # same directory, same disk: a rename, never a copy
                    full = os.path.join(self.root, p)
                    tmp = os.path.join(os.path.dirname(full), f".{os.path.basename(full)}.webbee-hold-{os.getpid()}")
                    os.replace(full, tmp)
                    moved.append((tmp, full))
                r = self._git("reset", "--hard", goal)
            finally:
                for tmp, full in moved:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    os.replace(tmp, full)
            if r.returncode != 0:
                return {"ok": False, "content": f"rollback failed: {r.stderr.strip()[:500]}"}
            nested, t_uncovered = self._read_body(target)
        except Exception as e:
            return {"ok": False, "content": f"rollback failed: {type(e).__name__}: {e}"}
        lines = [f"restored the workspace to checkpoint {to} ({target[:9]})"
                 + (f", including the files inside the nested repositories {', '.join(nested)}." if nested else ".")]
        not_restored = [(g + "/", "this checkpoint holds only a pointer to the nested repository, not its "
                                  "files (taken before nested repositories were snapshotted); left as it is now")
                        for g in gitlinks] + t_uncovered
        if not_restored:
            lines.append(f"NOT restored (checkpoint {to} does not hold them):")
            lines += [f"  - {p} -- {why}" for p, why in not_restored[:_NAMED_CAP]]
        if held:
            lines.append("Left as they are now (the safety snapshot could not save them, so the rollback "
                         "did not overwrite them):")
            lines += [f"  - {p} -- {unsaved.get(p, 'not in the safety snapshot (ignored or excluded)')}"
                      for p in held[:_NAMED_CAP]]
        lines.append(f"Note: {_SKIP_RULE}; a rollback leaves them as they are.")
        lines.append(f"The previous state -- including any files this rollback removed -- "
                     f"was saved first as checkpoint cp-{undo.get('n', '?')} "
                     f"({undo.get('id', '?')}), so this rollback is itself undoable.")
        return {"ok": True, "content": "\n".join(lines),
                "nested": nested, "not_restored": [p for p, _w in not_restored], "left_as_is": held}

    _AUTO_FAIL_LATCH = 3   # consecutive AUTO failures before pausing auto-checkpointing

    def note_auto_result(self, ok: bool) -> None:
        """Record an AUTO (pre-write) snapshot outcome. A single transient
        failure must NOT disable the session's time machine (final-review F8):
        latch auto-checkpointing OFF only after _AUTO_FAIL_LATCH CONSECUTIVE
        failures; any success resets the streak and re-enables it. A snapshot
        that ran past the time budget pauses it at once (checkpoint())."""
        if ok:
            self._auto_fail_streak = 0
            self.auto_ok = True
            self.pause_reason = ""
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
            why = (f" ({self.pause_reason})" if self.pause_reason else " after repeated shadow errors")
            body = (f"⚠ Auto-checkpointing is paused{why} — "
                    "manual /checkpoint + /rollback still work.\n") + body
        return body
