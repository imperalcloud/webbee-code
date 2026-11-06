"""IntelService -- owns the index + graph + freshness for one repo. This is
the object injected everywhere (executor/session/repl): `.build()` runs the
cold path (cache-load or full index) off the event loop, `.apply_changes()`
does the cheap incremental re-parse the watcher drives, and `.repo_profile()`
is the bounded summary handed to the brain."""
from __future__ import annotations
import asyncio
import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter

from imperal_sdk import code_index as indexer
from imperal_sdk.code_index import ProjectIndex
from webbee.intel import store
from webbee.intel.graph import CodeGraph

log = logging.getLogger("webbee.intel")

# The cloud code map follows the workspace by what actually changed and what the vault confirmed -- never by a clock
# (the owner, 2026-10-30: "not by seconds -- by the real sync and its confirmation"). Every tool call compares each
# repository's files (path, size, time) with the listing the vault last confirmed; only a repository that differs is
# sent, and the kernel's sync request waits for that sync to end and answers with what the vault took.

# The index cache is saved this long after the first unsaved change (a branch switch is hundreds of changes: one save).
# Lost at a hard quit costs nothing wrong: the next start's stat check re-parses what the cache missed.
SAVE_DEBOUNCE_S = 10.0

# 2026-10-30, the owner's folder of 7 repositories: every code question after an edit synced all 7, 2-4 s each with
# nothing changed -- a full sync now asks the cloud nothing for a repository whose files are the ones the vault last
# confirmed.

_MAX_PROFILE_SAMPLE = 20
_MAX_CONTRACT_EVIDENCE = 5000
_TEST_HINT_FILES = ("pytest.ini", "tox.ini", "package.json", "Makefile", "pyproject.toml")


def _git_ref(root: str) -> str:
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# Dependency / build trees are pruned from the index walk. This is not only a
# speed win, it is a CORRECTNESS one: `_walk` stops at `limit`, so on a repo
# with a big in-tree dependency directory the cap could be spent on
# third-party files while the project's OWN sources went unindexed.
#
# Measured on this checkout (it carries an in-tree `venv/`): 3062 files -> 111
# candidates, and build_index 30.0s -> well under a second.
#
# ".git" and every DOTTED directory were already pruned (covering .venv/,
# .tox/, .mypy_cache/, ...). These are the undotted ones that were not.
# Deliberately conservative -- only names that are unambiguous dependency
# caches. Names that a real project might legitimately use for its OWN source
# ("build", "dist", "target", "env", "vendor") are NOT pruned by name here: a
# virtualenv called "venv"/"env" is instead detected by its `pyvenv.cfg`
# marker below, which is a fact about the directory rather than a guess about
# its name.
_SKIP_DIRS = frozenset({"node_modules", "site-packages", "__pycache__"})
# macOS packages: an app's or a media library's insides, not the person's files (universal index, 2026-11-04)
_APP_PACKAGES = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".photolibrary", ".aplibrary",
                 ".migratedphotolibrary", ".imovielibrary", ".fcpbundle", ".logicx", ".band")


def _is_virtualenv(path: str) -> bool:
    """True for a real Python virtualenv root: PEP 405 puts `pyvenv.cfg` at
    the top of every one. Checking the MARKER (not the folder name) means a
    project module that merely happens to be called `env/` keeps being
    indexed, while an in-tree `venv/` is skipped whatever it is named."""
    return os.path.exists(os.path.join(path, "pyvenv.cfg"))


def skipped_dir(parent: str, name: str) -> bool:
    """The ONE rule for what the index never reads (the full walk and the watcher alike):
    every dotted directory (.git, .next build output, .venv, caches, .webbee), the
    dependency caches, and any virtualenv by its marker."""
    return (name.startswith(".") or name in _SKIP_DIRS or name.endswith(_APP_PACKAGES)
            or _is_virtualenv(os.path.join(parent, name)))


def ignored_path(root: str, path: str) -> bool:
    """True when ``path`` (absolute or root-relative) lies under a directory skipped_dir skips.
    Normalized to forward slashes so Windows paths filter too."""
    p = path.replace("\\", "/").replace(os.sep, "/")
    rel = p
    for r in dict.fromkeys([root, os.path.realpath(root)] if root else []):
        r = r.replace(os.sep, "/").rstrip("/")
        if p.startswith(r + "/"):
            rel = p[len(r) + 1:]
            break
    else:
        if root and os.path.isabs(path):
            root = ""           # outside the workspace: judged by its own directory names only
    parts = [x for x in rel.split("/") if x][:-1]          # the directories, not the file itself
    parent = root or ""
    for name in parts:
        if name in (".", ".."):
            continue
        if skipped_dir(parent, name) if root else (name.startswith(".") or name in _SKIP_DIRS):
            return True
        parent = os.path.join(parent, name)
    return False


def _stats_of(root: str, rels) -> dict:
    """``{relpath: [mtime_ns, size]}`` -- stat only, no read: cheap on a big repo."""
    out = {}
    for rel in rels:
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            continue
        out[rel] = [st.st_mtime_ns, st.st_size]
    return out


def _walk(root: str, limit: int = 20000) -> list[str]:
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not skipped_dir(dp, d)]
        for fn in fns:
            out.append(os.path.relpath(os.path.join(dp, fn), root))
            if len(out) >= limit:
                return out
    return out


# ---- universal index (phase 2, 2026-11-04): any folder, every file kind, the focus first ----
UNIT_MAX_FILES = 100_000       # one index unit (spec 2026-11-03 §3.2); a bigger folder is split by imperal-mcp
BUILD_BATCH = 2_000            # files per published step of a first build: answers come before the whole folder
POOL_MIN_FILES = 500           # below this a build stays in-process (a pool's start costs more than it saves)
RECENT_S = 30 * 86400          # "changed lately" -- indexed right after the focus


def _git_listed(root: str) -> set | None:
    """The files git tracks or would track in ``root`` (``ls-files -co --exclude-standard``: a .gitignore'd file is
    never read -- the same rule as the cloud listing), or None when ``root`` is no git repository / git cannot say."""
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {p.replace(os.sep, "/") for p in r.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def _index_listing(root: str, focus: str | None = None, limit: int = UNIT_MAX_FILES,
                   exclude: tuple = ()) -> tuple[list, dict, dict]:
    """``(the folder's files the index reads, in priority order; {relpath: [mtime_ns, size]}; {"placeholder": n,
    "over_cap": n})`` -- ONE listing for the first build, the cache check and every refresh. Order: the focus folder,
    files changed in the last 30 days (newest first), then by path. Never read: what ``kind_of`` refuses by the
    file's name and its real path (secrets, caches, dependency trees), a .gitignore'd file of a git repository,
    anything but a regular file, and cloud placeholders (reading one downloads it). ``exclude``: root-relative
    folders another unit indexes (a repository inside a collection), never walked here."""
    from imperal_sdk.universal.index import is_placeholder
    from imperal_sdk.universal.kinds import kind_of
    import stat as _stat
    found: list[tuple[str, float]] = []
    stats: dict = {}
    skipped = {"placeholder": 0, "over_cap": 0}
    excluded = {str(e).strip("/") for e in exclude if str(e).strip("/")}
    listed = _git_listed(root)
    # no git answer (a folder that is no repository, the repositories inside it, a machine without git -- Windows,
    # a minimal Linux): every .gitignore on the way applies, read here (06.11 report: none applied, ~19.6k dump and
    # screenshot files were indexed). The same parser as the SFTP listing (imperal_sdk.code_index.gitignore_rules).
    from imperal_sdk.code_index import gitignore_rules, gitignored
    rules: list | None = None if listed is not None else []
    for dp, dns, fns in os.walk(root):
        here = os.path.relpath(dp, root).replace(os.sep, "/")
        if rules is not None and ".gitignore" in fns:
            try:
                with open(os.path.join(dp, ".gitignore"), encoding="utf-8", errors="replace") as fh:
                    rules = rules + gitignore_rules(fh.read(), "" if here == "." else here)
            except OSError:
                pass
        dns[:] = sorted(d for d in dns if not skipped_dir(dp, d)
                        and (d if here == "." else f"{here}/{d}") not in excluded
                        and not (rules and gitignored(rules, d if here == "." else f"{here}/{d}", True)))
        for fn in sorted(fns):
            rel = fn if here == "." else f"{here}/{fn}"
            if is_placeholder(rel):
                skipped["placeholder"] += 1
                continue
            if listed is not None and rel not in listed:
                continue
            if rules and gitignored(rules, rel, False):
                continue
            if kind_of(rel, root=root) is None:
                continue
            try:
                st = os.lstat(os.path.join(dp, fn))
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode):          # a link, a FIFO, a socket: never read
                continue
            if is_placeholder(rel, st):
                skipped["placeholder"] += 1
                continue
            if len(found) >= limit:
                skipped["over_cap"] += 1
                continue
            found.append((rel, st.st_mtime))
            stats[rel] = [st.st_mtime_ns, st.st_size]
    prefix = (focus or "").strip("/")
    now = time.time()

    def rank(item):
        rel, mtime = item
        in_focus = bool(prefix) and (rel == prefix or rel.startswith(prefix + "/"))
        recent = now - mtime <= RECENT_S
        return (0 if in_focus else 1, 0 if recent else 1, -mtime if recent else 0.0, rel)
    return [rel for rel, _m in sorted(found, key=rank)], stats, skipped


def _index_walk(root: str, focus: str | None = None, limit: int = UNIT_MAX_FILES,
                exclude: tuple = ()) -> tuple[list[str], dict]:
    """``(files in priority order, skipped counts)`` -- ``_index_listing`` without the stats."""
    rels, _stats, skipped = _index_listing(root, focus, limit, exclude)
    return rels, skipped


POOL_MAX_WORKERS = 4           # parsing holds up to ~0.3 GB per job (imperal_sdk.universal.index.RAW_CAP)
POOL_CHUNK = 50                # files per pool job
CHUNK_TIMEOUT_S = 120.0        # one job; past it the job's files are retried one by one
FILE_TIMEOUT_S = 30.0          # one file; past it the file is skipped as ``timeout``


def _kill_pool(pool) -> None:
    """A pool with a stuck job: its processes killed (a hung extractor never ends by itself), the rest cancelled."""
    for proc in list((getattr(pool, "_processes", None) or {}).values()):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def _build_chunk(root: str, rels: list) -> dict:
    """One process-pool job: these files' index entries (the pool's children run at a lower priority)."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    return indexer.build_index(root, rels).files


class IntelService:
    def __init__(self, root: str, repo_key: str, cache_dir: str = "", focus: str | None = None,
                 exclude: tuple = ()) -> None:
        self.root = os.path.realpath(root)
        self.focus = focus
        self.exclude = tuple(exclude)
        self._progress = {"indexed": 0, "total": 0, "skipped": {"placeholder": 0, "over_cap": 0, "unread": 0},
                          "focus_done": False}
        self.repo_key = repo_key
        self.cache_dir = cache_dir or os.path.expanduser("~/.cache/webbee/intel")
        self.index: ProjectIndex | None = None
        self.graph: CodeGraph | None = None
        self.git_ref = ""
        self.fresh = False
        self.vectors: VectorStore | None = None
        self.vectors_ready = False
        self._backend = "__unset__"          # sentinel -- distinct from a real None result
        self._chunk_hashes: dict[str, str] = {}  # chunk id -> content_hash, for incremental skip
        # Guards the index/graph mutation (apply_changes, driven by the
        # watcher off-loop via asyncio.to_thread) against a concurrent
        # repo_profile() read -- without it, a worker thread can resize
        # self.index.files mid-iteration on the reader's side.
        self._lock = threading.Lock()
        # code graph everywhere (plan 3 Task 7): the client is a source of the vault's graph -- a zero-arg
        # factory of the cloud client (set by the tool executor); None = no cloud, nothing is synced
        self.vault_client_factory = None
        self._sync_lock = threading.Lock()
        self._synced_sigs: dict = {}      # root -> (listing signature, repo_key, when) of the last sync the vault took
        self.last_sync: dict = {}
        self.last_sync_ok_at = 0.0         # the last sync the vault took (time.time()); 0 -- none this session
        self.last_sync_error = ""          # why the last sync failed; "" -- it did not
        self.stale_reason = ""             # why the index is not known fresh; "" -- it is (or not built)
        self._stats: dict = {}             # relpath -> [mtime_ns, size] of what the index holds, saved with it
        self._tree: dict = {}              # dir -> hash over self._stats (tree.dir_hashes); {} -- compute it again
        self._checksum = ""                # tree.repo_checksum at the last check ("" -- no git: walk)
        self._pool_job = None              # the pool job (None: _build_chunk); a test puts a stuck one here
        self._vectors_dirty = False
        self._save_guard = threading.Lock()
        self._save_timer = None
        # ONE sync worker: requests merge while it runs (a full listing supersedes changed paths); before, every
        # watcher batch started its own thread and they piled up on _sync_lock behind a long first sync
        self._sync_cv = threading.Condition()
        self._sync_worker = None
        self._sync_running = False
        self._sync_full = False            # a full listing is pending
        self._sync_history = False         # ... with the git history's links
        self._sync_rels: dict = {}         # changed paths pending (insertion-ordered)
        self._sync_taken = 0               # requests the worker took, numbered
        self._sync_done = 0                # the last one it finished
        self._sync_out: dict = {}          # what that one returned

    def _get_backend(self):
        # Lazy + cached: the model load is expensive (and may be unavailable
        # entirely), so it happens at most once per service instance. A
        # None result is cached too -- unavailable stays unavailable rather
        # than retrying the load on every call. The load is guarded too: an
        # install without numpy/model2vec (the sdk's embedder,
        # ``imperal_sdk.code_index.load_backend``) must degrade to "no
        # backend", not crash build() and disable intel entirely.
        if self._backend != "__unset__":
            return self._backend
        try:
            self._backend = indexer.load_backend()
        except Exception:
            self._backend = None
        return self._backend

    @property
    def ready(self) -> bool:
        return self.graph is not None

    def build(self) -> None:
        """Load cache (fast) else full index; then rebuild the graph. Sync --
        call via asyncio.to_thread from the repl boot. A cache is checked
        against the disk by stat alone (live 2026-10-30: the cache of the first
        build for a HEAD was loaded as is -- edits made while Webbee was closed
        and uncommitted edits stayed invisible after a restart at that HEAD):
        only the files whose mtime or size differ are re-parsed, files gone
        are dropped, new ones added."""
        touched, rebuilt = self.load_or_build()
        self._embed_build(self.index, touched)
        if rebuilt:
            self._save_cache()             # this index for the next start (a reconciled one too)
        self.sync_vault()                 # code graph everywhere: the whole repo into the vault

    def load_or_build(self) -> tuple[dict | None, bool]:
        """The index from the cache checked against the disk, else built: ``(touched, changed)`` -- ``touched`` the
        files the check re-read or dropped (None after a full build), ``changed`` whether the index differs from the
        cache (it should be saved). The check compares the checksum trees top down (``tree.changed_files``): a
        directory whose hash is unchanged is skipped whole. A folder without git is cached too (``store.NO_GIT``)."""
        from webbee.intel import tree
        self.git_ref = _git_ref(self.root)
        checksum = tree.repo_checksum(self.root)
        walked, stats, skipped = _index_listing(self.root, self.focus, exclude=self.exclude)
        self._progress = {"indexed": 0, "total": len(walked), "focus_done": False,
                          "skipped": {**skipped, "unread": 0, "timeout": 0}}
        cached = store.load_with_stats(self.cache_dir, self.repo_key, self.git_ref)
        new_tree = tree.dir_hashes(stats)
        touched = None                      # None: a full index (no cache); else the files the disk changed
        if cached is None:
            idx = self._build_progressive(walked)
        else:
            idx, old_stats = cached
            changed, gone, _compared = tree.changed_files(old_stats, stats,
                                                          store.load_tree(self.cache_dir, self.repo_key), new_tree)
            gone = sorted(set(gone) | (set(idx.files) - set(stats)))    # a cached file no longer listed
            reparsed = self._build_files(changed) if changed else {}
            for rel in changed:             # undecodable now: out of the index, as a full build leaves it
                idx.files.pop(rel, None)
            idx.files.update(reparsed)
            for rel in gone:
                idx.files.pop(rel, None)
            touched = {rel: idx.files.get(rel) for rel in changed + gone}
        idx.git_ref = self.git_ref
        with self._lock:
            self._stats = stats
            self._tree = new_tree
            self._checksum = checksum
            self.index = idx
            self.graph = CodeGraph(idx)
        self._progress.update(indexed=len(walked), focus_done=True)     # every listed file processed (read or unread)
        self.fresh = True
        self.stale_reason = ""
        return touched, cached is None or bool(touched)

    def checksum(self) -> str:
        """The folder's checksum: the root of its checksum tree (``tree[""]``) -- equal checksums, equal listings."""
        return (self._tree or {}).get("", "")

    def refresh(self) -> dict:
        """The index catches up with the disk without a watcher: a git repository whose checksum (HEAD, status, the
        stats of what it names) is unchanged answers at once, with no walk; else one listing, its checksum tree
        compared top down with the index's, and only the changed files read. A file that left the listing but is
        still on disk (now .gitignore'd, now named like a secret) leaves the index unread.
        ``{changed, gone, unlisted, compared}`` or ``{"unchanged": True}``."""
        from webbee.intel import tree
        if self.index is None:
            return {}
        checksum = tree.repo_checksum(self.root)
        if checksum and checksum == self._checksum:
            return {"unchanged": True}
        walked, stats, _skipped = _index_listing(self.root, self.focus, exclude=self.exclude)
        new_tree = tree.dir_hashes(stats)
        with self._lock:
            old_stats, old_tree = dict(self._stats), dict(self._tree or {})
        changed, gone, compared = tree.changed_files(old_stats, stats, old_tree, new_tree)
        with self._lock:
            gone = sorted(set(gone) | (set(self.index.files) - set(stats)))
        deleted = [rel for rel in gone if not os.path.lexists(os.path.join(self.root, rel))]
        unlisted = [rel for rel in gone if rel not in set(deleted)]
        if unlisted:
            self.evict(unlisted)
        if changed or deleted:
            self.apply_changes(changed + deleted)
        with self._lock:
            self._stats = dict(stats)
            self._tree = new_tree
            self._checksum = checksum
        self._progress["total"] = len(walked)
        self._progress["indexed"] = len(walked)
        if unlisted and not (changed or deleted):
            self._save_soon()
        return {"changed": len(changed), "gone": len(gone), "unlisted": len(unlisted), "compared": compared}

    def evict(self, rels: list) -> None:
        """Files out of the index without a read (the lock and the graph rebuild apply_changes uses)."""
        with self._lock:
            for rel in rels:
                if self.index is not None:
                    self.index.files.pop(rel, None)
                self._stats.pop(rel, None)
            self._tree = {}
            if self.index is not None:
                self.graph = CodeGraph(self.index)

    def _build_files(self, rels: list) -> dict:
        """These files' index entries. A small set of code stays in-process; documents (any extractor can meet a
        hostile file) and big sets go to a process pool whose stuck jobs are killed (``_build_in_pool``) -- one hung
        file never stops the build (final review 2026-11-04)."""
        if not rels:
            return {}
        from imperal_sdk.universal.kinds import kind_of
        if len(rels) < POOL_MIN_FILES and all(kind_of(r) in ("code", "config") for r in rels):
            return indexer.build_index(self.root, rels).files
        try:
            return self._build_in_pool(rels)
        except Exception as exc:          # no pool here (a sandbox, a frozen app): the same work in-process
            log.info("index pool unavailable, building in-process: %s", exc)
            return indexer.build_index(self.root, rels).files

    def _build_in_pool(self, rels: list) -> dict:
        """Jobs of ``POOL_CHUNK`` files on at most ``POOL_MAX_WORKERS`` processes; a job past ``CHUNK_TIMEOUT_S`` (or
        whose process died) has its pool killed and its files retried one per job; a single file past
        ``FILE_TIMEOUT_S`` is skipped as ``timeout`` (counted in ``progress``)."""
        workers = max(1, min(POOL_MAX_WORKERS, (os.cpu_count() or 2) // 2))
        size = POOL_CHUNK if len(rels) >= POOL_CHUNK * workers else max(1, -(-len(rels) // workers))
        out: dict = {}
        hung = self._run_pool([rels[i:i + size] for i in range(0, len(rels), size)], workers, CHUNK_TIMEOUT_S, out)
        if hung:
            still = self._run_pool([[r] for part in hung for r in part], workers, FILE_TIMEOUT_S, out)
            for part in still:
                log.warning("index: %s did not finish in %ss -- skipped", part[0], FILE_TIMEOUT_S)
                self._progress.setdefault("skipped", {}).setdefault("timeout", 0)
                self._progress["skipped"]["timeout"] += 1
        return out

    def _run_pool(self, parts: list, workers: int, timeout: float, out: dict) -> list:
        """Run ``parts`` in waves of ``workers`` jobs (each job's clock starts with it); the parts that did not end
        in ``timeout`` or whose process died. A pool that cannot start raises (the caller builds in-process)."""
        from concurrent.futures import ProcessPoolExecutor, wait
        hung: list = []
        pending = list(parts)
        pool = None
        try:
            while pending:
                if pool is None:
                    pool = ProcessPoolExecutor(max_workers=workers)
                wave, pending = pending[:workers], pending[workers:]
                job = getattr(self, "_pool_job", None) or _build_chunk      # tests put a stuck job here
                futs = {pool.submit(job, self.root, part): part for part in wave}
                done, not_done = wait(futs, timeout=timeout)
                broken = False
                for f in done:
                    try:
                        out.update(f.result())
                    except Exception:     # the job's process died (a crash, out of memory): its files are retried
                        hung.append(futs[f])
                        broken = True
                hung.extend(futs[f] for f in not_done)
                if not_done or broken:
                    _kill_pool(pool)
                    pool = None
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
        return hung

    def _build_progressive(self, walked: list) -> ProjectIndex:
        """A first build in published steps: the focus folder first, then ``BUILD_BATCH`` files at a time -- the index
        answers (and says how far it is: ``progress``) long before a home folder is done."""
        idx = ProjectIndex(git_ref=self.git_ref)
        prefix = (self.focus or "").strip("/")
        n_focus = sum(1 for r in walked if prefix and (r == prefix or r.startswith(prefix + "/")))
        steps = ([walked[:n_focus]] if n_focus else []) + [walked[i:i + BUILD_BATCH]
                                                           for i in range(n_focus, len(walked), BUILD_BATCH)]
        done = 0
        for k, step in enumerate(steps):
            files = self._build_files(step)
            done += len(step)
            idx.files.update(files)
            self._progress["skipped"]["unread"] += len(step) - len(files)
            self._progress["indexed"] = done            # processed so far; ``skipped.unread`` says how many were not readable
            if done >= n_focus:
                self._progress["focus_done"] = True
            if k == 0 or k % 5 == 0 or k == len(steps) - 1:
                with self._lock:              # published: search and outline see what is built so far
                    self.index = idx
                    self.graph = CodeGraph(idx)
        return idx

    def progress(self) -> dict:
        """How far the index is: ``{indexed, total, skipped: {placeholder, over_cap, unread}, focus_done}`` --
        ``indexed`` counts the listed files processed (``unread`` of them could not be read: encrypted, broken, over
        their cap); ``indexed == total`` means the build is done."""
        return {**self._progress, "skipped": dict(self._progress["skipped"])}

    def _embed_build(self, idx: ProjectIndex, touched: dict | None = None) -> None:
        # Off-loop already (build() is called via asyncio.to_thread from the
        # repl boot): cache-load skips re-embedding entirely when the
        # git_ref+model_id match; a miss chunks+embeds+saves. Fail-soft --
        # no backend means self.vectors stays None and search_code (F5)
        # falls back to lexical+graph instead of raising. ``touched`` -- the
        # files the stat check re-parsed: their cached vectors are re-embedded.
        from webbee.intel import store as _store
        b = self._get_backend()
        if b is None:
            self.vectors = None
            self.vectors_ready = False
            return
        # vectors.py has its own module-level numpy -- import it only now
        # that a backend is confirmed present, not unconditionally at the
        # top of this function (an intel-only install with no backend must
        # never need numpy at all).
        from webbee.intel.vectors import VectorStore
        cached = _store.load_vectors(self.cache_dir, self.repo_key, self.git_ref, b.model_id)
        if cached is not None:
            ids, mat = cached
            vs = VectorStore.from_arrays(b.dim, ids, mat)
        else:
            touched = None                  # embedded from the current index: nothing left to re-embed
            chunks = indexer.chunk_index(self.root, idx)
            vs = VectorStore(b.dim)
            if chunks:
                vecs = b.embed([c.text for c in chunks])
                vs.add([c.id for c in chunks], vecs)
                self._chunk_hashes = {c.id: c.content_hash for c in chunks}
                ids, mat = vs.to_arrays()
                try:
                    _store.save_vectors(self.cache_dir, self.repo_key, self.git_ref, b.model_id, ids, mat)
                except OSError:
                    pass
        with self._lock:
            self.vectors = vs
            self.vectors_ready = True
        if touched:
            embedded = self._embed_updates(touched)
            with self._lock:
                self._swap_vectors(touched, *embedded)

    def _embed_updates(self, updates: dict):
        """``(fresh_ids, fresh_vecs, current_ids)`` for the touched files -- compute only, off the lock."""
        fresh_ids: list = []
        fresh_vecs = None
        current_ids: dict[str, set] = {}   # edited rel -> its FULL current chunk-id set
        b = self._get_backend()
        fresh_chunks = []
        for rel, fi in updates.items():
            if fi is None:
                continue  # deleted -- current_ids[rel] stays absent, so ALL its
                          # ids are caught as stale below
            chunks = indexer.chunk_file(self.root, fi)
            current_ids[rel] = {c.id for c in chunks}
            for c in chunks:
                if self._chunk_hashes.get(c.id) != c.content_hash:
                    fresh_chunks.append(c)
        if fresh_chunks:
            fresh_vecs = b.embed([c.text for c in fresh_chunks])
            fresh_ids = [c.id for c in fresh_chunks]
            for c in fresh_chunks:
                self._chunk_hashes[c.id] = c.content_hash
        return fresh_ids, fresh_vecs, current_ids

    def _swap_vectors(self, updates: dict, fresh_ids, fresh_vecs, current_ids) -> None:
        """The touched files' vectors swapped in -- the caller holds ``_lock``."""
        # F1: a touched file's vanished chunk ids -- span-shifted by
        # the edit, or all of them when the file was deleted -- are
        # orphans and must be dropped before the fresh ones go in.
        # Guarding on `not in current_ids` (never a naive "remove
        # all `{rel}#` then add fresh") keeps the hash-skip
        # optimization correct: an edit to a LATE symbol must not
        # drop an EARLIER unchanged (and therefore skipped) chunk
        # that's still current.
        stale = [i for i in self.vectors.ids()
                 if "#" in i
                 and i.split("#", 1)[0] in updates                          # a touched file
                 and i not in current_ids.get(i.split("#", 1)[0], set())]   # no longer a current chunk
        if stale:
            self.vectors.remove(stale)
            for i in stale:
                self._chunk_hashes.pop(i, None)
        if fresh_ids:
            self.vectors.add(fresh_ids, fresh_vecs)
        if stale or fresh_ids:
            self._vectors_dirty = True

    def apply_changes(self, rel_paths) -> None:
        # Parse off-lock (file I/O + tree-sitter parse don't touch shared
        # state), then apply the mutation + graph rebuild atomically under
        # the lock -- a concurrent repo_profile() read must never observe a
        # partially-updated index.
        if self.index is None:
            return
        updates = {}
        stats = {}
        to_read: list = []
        for rel in rel_paths:
            ap = os.path.join(self.root, rel)
            if not os.path.exists(ap):
                updates[rel] = None  # deleted -> drop from the index
                stats[rel] = None
                continue
            try:
                st = os.stat(ap)        # before the read: an edit after it differs at the next start
            except OSError:
                continue
            from imperal_sdk.universal.index import is_placeholder
            from imperal_sdk.universal.kinds import kind_of
            if kind_of(rel, root=self.root) is None or is_placeholder(rel, st):
                updates[rel] = None     # the same gates as the walk: never read, out of the index
                stats[rel] = None
                continue
            to_read.append(rel)
            stats[rel] = [st.st_mtime_ns, st.st_size]
        # the same reading as a build (every kind through its view, a hung file killed); not readable now: out
        read = self._build_files(to_read) if to_read else {}
        for rel in to_read:
            updates[rel] = read.get(rel)

        # Embed compute (chunk + backend inference) stays off-lock, mirroring
        # the parse-then-swap pattern above -- only the vector-store mutation
        # happens under `_lock`. Skipped entirely when there's no live vector
        # store or the backend is unavailable (fail-soft, F5's fallback).
        embed_ready = self.vectors is not None and self._get_backend() is not None
        embedded = self._embed_updates(updates) if embed_ready else None

        # HEAD moved (a pull, a checkout, a commit): the cache goes under the new one (cheap: one rev-parse)
        ref = _git_ref(self.root)
        self.sync_vault(list(updates))    # only the changed files go to the vault
        with self._lock:
            for rel, fi in updates.items():
                if fi is None:
                    self.index.files.pop(rel, None)
                else:
                    self.index.files[rel] = fi
            for rel, st in stats.items():
                if st is None:
                    self._stats.pop(rel, None)
                else:
                    self._stats[rel] = st
            if ref and ref != self.git_ref:
                self.git_ref = ref
                self.index.git_ref = ref
                self._vectors_dirty = True     # the vector cache is keyed by HEAD too
            self.graph = CodeGraph(self.index)
            if embedded is not None:
                self._swap_vectors(updates, *embedded)
            self._tree = {}                    # the stats moved: the tree is computed again at the next check
        if updates:
            self._save_soon()

    # ── the index cache kept current: saved after changes, debounced ─────────

    def _save_soon(self) -> None:
        """Save the cache ``SAVE_DEBOUNCE_S`` after the first unsaved change -- never once per change (a branch
        switch is one write); not pushed back by later changes, so a session that edits without pause still saves."""
        with self._save_guard:
            if self._save_timer is not None:
                return
            t = threading.Timer(SAVE_DEBOUNCE_S, self._save_from_timer)
            t.daemon = True
            t.name = "webbee-index-save"
            self._save_timer = t
        t.start()

    def _save_from_timer(self) -> None:
        with self._save_guard:
            self._save_timer = None
        self._save_cache()

    def flush_cache(self) -> None:
        """Save a pending change now (a quit, a test) instead of at the timer."""
        with self._save_guard:
            t, self._save_timer = self._save_timer, None
        if t is not None:
            t.cancel()
            self._save_cache()

    def _save_cache(self) -> None:
        """The index, the stats of its files and (when they changed) the vectors, into the cache. Fail-soft."""
        if self.index is None:
            return
        from webbee.intel import tree
        with self._lock:
            snap = ProjectIndex(git_ref=self.git_ref)
            snap.files = dict(self.index.files)
            stats = dict(self._stats)
            if not self._tree:
                self._tree = tree.dir_hashes(stats)
            dirs = dict(self._tree)
            vecs = None
            if self._vectors_dirty and self.vectors is not None:
                vecs = self.vectors.to_arrays()
                self._vectors_dirty = False
        try:
            store.save(self.cache_dir, self.repo_key, snap, stats, dirs)
            b = self._get_backend() if vecs is not None else None
            if b is not None:
                store.save_vectors(self.cache_dir, self.repo_key, snap.git_ref, b.model_id, *vecs)
        except Exception as exc:        # a full disk or a gone cache dir never touches the live index
            log.warning("index cache save failed: %s", exc)

    # ── freshness: the watcher reports its end ───────────────────────────

    def watcher_ended(self, reason: str) -> None:
        """The file watcher stopped (watchfiles missing, the OS refused, it crashed): an edit from now on reaches the
        index only through the agent's own writes -- the index is no longer known fresh, and says why."""
        self.fresh = False
        self.stale_reason = f"the file watcher stopped ({reason}): edits made since may be missing from the index"

    def _sync_vault(self, rels=None, history: bool = True) -> dict:
        """The workspace (``rels=None``: every indexable file, with the git history's co-change links) or the
        changed files into the vault. Fail-soft: a cloud outage never touches the local index."""
        factory = self.vault_client_factory
        if factory is None:
            return {}
        if is_repository(self.root):
            return self._sync_root(self.root, rels, history)
        # never the whole of a home folder or a folder of projects (live: a 14 GB folder with photo backups) -- its
        # repositories one by one (live 2026-10-25: Webbee in a folder of repositories failed the sync every step)
        subs = sub_repositories(self.root)
        if not subs:
            return {"repo_key": "", "partial_reason": f"{self.root} is not a repository (no .git, no project manifest) "
                                                      "and holds none"}
        if rels is not None:            # a change: only the repositories it touched, with their own paths
            by_repo: dict = {}
            for rel in rels:
                head, _, tail = str(rel).replace(os.sep, "/").partition("/")
                if head in subs and tail:
                    by_repo.setdefault(head, []).append(tail)
            todo = {name: by_repo[name] for name in by_repo}
        else:
            todo = {name: None for name in subs}
        repos = {}
        for name, sub_rels in todo.items():
            out = self._sync_root(os.path.join(self.root, name), sub_rels, history)
            if out.get("repo_key"):
                repos[name] = str(out["repo_key"])
        out = {"repo_key": "", "repos": repos}
        if repos:
            self.last_sync = out
            self.last_sync_ok_at = time.time()
            self.last_sync_error = ""
        elif todo:
            self.last_sync_error = self.last_sync_error or "no repository of this folder reached the code map"
        return out

    def _sync_root(self, root: str, rels=None, history: bool = True) -> dict:
        """One repository (``root``) into the vault: every indexable file, or ``rels`` (paths under ``root``)."""
        factory = self.vault_client_factory
        with self._sync_lock:
            t0 = time.monotonic()
            try:
                from webbee.repo import compute_repo_key

                async def read(paths):
                    got = {}
                    base = os.path.realpath(root)
                    for p in paths:
                        full = os.path.join(root, p)
                        # never a link, never a path that resolves outside the workspace
                        real = os.path.realpath(full)
                        if os.path.islink(full) or not (real == base or real.startswith(base + os.sep)):
                            continue
                        try:
                            with open(full, "rb") as f:
                                got[p] = f.read(indexer.MAX_FILE_BYTES + 1)
                        except OSError:
                            continue
                    return got
                meta = {"machine": {"machine_id": machine_id()}, "root": root, "root_path": root,
                        "os": "windows" if os.name == "nt" else "posix", "source": "local", "ref": "",
                        "machine_kind": "local", "git": local_git_identity(root),
                        "local_key": compute_repo_key(root), "listing_complete": rels is None}
                listing, capped = _local_listing_of(root, rels)
                if capped:      # an incomplete listing never lets the vault drop the files it did not name
                    meta.update(listing_complete=False, partial_reason=capped)
                sig = _listing_sig(listing, capped) if rels is None else ""
                seen = self._synced_sigs.get(root)
                if sig and seen and seen[0] == sig and not history:
                    # its files are the ones the vault last confirmed: no cloud round trip
                    self.last_sync_ok_at = time.time()
                    self.last_sync_error = ""
                    out = {"repo_key": seen[1], "listed": len(listing), "changed": 0, "skipped": "unchanged"}
                    self.last_sync = out
                    return out
                measured = co_change_pairs(root) if rels is None and history else None
                out = asyncio.run(vault_sync(factory(), meta, listing, read, measured))
                if sig and out.get("repo_key"):
                    self._synced_sigs[root] = (sig, str(out["repo_key"]), time.time())
                elif rels is not None:
                    self._synced_sigs.pop(root, None)       # changed files went: the next full sync asks the cloud
            except Exception as exc:
                log.warning("code sync to the vault failed: %s", exc)
                self.last_sync_error = f"{type(exc).__name__}: {exc}"[:300]
                return {}
            self.last_sync = out
            if out.get("repo_key"):
                self.last_sync_ok_at = time.time()
                self.last_sync_error = ""
            log.info("code sync repo=%s source=local listed=%d changed=%d read_bytes=%d new_blobs=%d extract_ms=%d "
                     "links+=%d links-=%d unresolved=%d partial=%s ms=%d", out.get("repo_key"), out.get("listed", 0),
                     out.get("changed", 0), out.get("read_bytes", 0), out.get("new_blobs", 0), out.get("extract_ms", 0),
                     out.get("links_added", 0), out.get("links_removed", 0), out.get("unresolved", 0),
                     out.get("partial_reason") or "-", int((time.monotonic() - t0) * 1000))
            return out


    def sync_now(self, wait_s: float | None = None) -> dict:
        """The kernel asks before a code question (frame phase A): the workspace reaches the vault now -- a full
        listing, so an edit no watcher reported (a shell edit) is sent too; the vault's plan picks the changed files.
        The git history is not re-read. It goes through the one sync worker and waits until ITS sync has ended --
        never a clock: the answer is what the vault confirmed (``{repo_key | repos, synced_at, ...counts}``), or
        ``{ok: False, content}`` when the sync really failed. While a repository's FIRST sync runs (a whole repo:
        extract + embed, minutes) it answers at once ``{pending}`` -- a state, not a failure. ``wait_s``: tests only."""
        if self.vault_client_factory is None:
            return {"ok": False, "content": "this session cannot reach the cloud"}
        if not self.last_sync and self._sync_busy():
            # the repo's first sync (a whole repo: extract + embed) is still running: answered at once, as a state
            return {"pending": "the first code sync of this workspace is still running -- the local index answers "
                               "until it ends"}
        ticket = self._sync_request(None, history=False)
        with self._sync_cv:
            done = self._sync_cv.wait_for(lambda: self._sync_done >= ticket, timeout=wait_s)
            out = dict(self._sync_out) if done else {}
        if not done:
            return {"pending": f"the code sync is still running after {wait_s:g}s"}
        if not out.get("repo_key") and not out.get("repos"):
            return {"ok": False, "content": str(out.get("partial_reason") or self.last_sync_error
                                                or "the code sync to the vault failed")}
        return {**out, "synced_at": time.time()}

    def sync_vault(self, rels=None, history: bool = True) -> None:
        """Off the caller's thread: the build and the watcher never wait for the cloud. Queued to the one sync
        worker, merged with what is already pending."""
        if self.vault_client_factory is None:
            return
        self._sync_request(rels, history)

    def _sync_busy(self) -> bool:
        """A sync runs or waits to run."""
        with self._sync_cv:
            queued = self._sync_running or self._sync_full or bool(self._sync_rels)
        return queued or self._sync_lock.locked()

    def _sync_request(self, rels, history: bool) -> int:
        """Queue a sync; the number of the worker's run that will carry it. A full listing supersedes the
        pending changed paths (it lists them too); changed paths beside a pending full one add nothing."""
        with self._sync_cv:
            if rels is None:
                self._sync_full = True
                self._sync_history = self._sync_history or history
                self._sync_rels.clear()
            elif not self._sync_full:
                self._sync_rels.update(dict.fromkeys(rels))
            ticket = self._sync_taken + 1
            if self._sync_worker is None and (self._sync_full or self._sync_rels):
                self._sync_worker = threading.Thread(target=self._sync_loop, name="webbee-code-sync", daemon=True)
                self._sync_worker.start()
            elif not (self._sync_full or self._sync_rels):
                ticket = self._sync_done   # nothing to send (no changed path): nothing to wait for
            return ticket

    def _sync_loop(self) -> None:
        """The one sync worker: takes what is pending, merged, until nothing is; then ends."""
        while True:
            with self._sync_cv:
                if not self._sync_full and not self._sync_rels:
                    self._sync_worker = None
                    self._sync_cv.notify_all()
                    return
                rels = None if self._sync_full else list(self._sync_rels)
                history = self._sync_history
                self._sync_full, self._sync_history = False, False
                self._sync_rels = {}
                self._sync_taken += 1
                run = self._sync_taken
                self._sync_running = True
            try:
                out = self._sync_vault(rels, history)
            except Exception as exc:       # never the worker's end: the next request still goes
                log.warning("code sync to the vault failed: %s", exc)
                self.last_sync_error = f"{type(exc).__name__}: {exc}"[:300]
                out = {}
            with self._sync_cv:
                self._sync_running = False
                self._sync_done = run
                self._sync_out = out or {}
                self._sync_cv.notify_all()

    def sync_if_due(self) -> bool:
        """Called on every tool call: off the caller's thread, the workspace is compared with what the vault last
        confirmed and only a repository whose files changed is sent (an unchanged one costs a local listing, no
        cloud call) -- never a second sync beside a running one. True when a check started."""
        if self.vault_client_factory is None or self.index is None:
            return False
        if self._sync_busy():
            return False
        self.sync_vault(None, history=False)
        return True

    def sync_state(self) -> dict:
        """How fresh the cloud code map of this workspace is, as this session knows it."""
        age = int(time.time() - self.last_sync_ok_at) if self.last_sync_ok_at else None
        return {"synced_s_ago": age, **({"sync_error": self.last_sync_error} if self.last_sync_error else {})}


    def attach_vault(self, client_factory) -> None:
        """The tool executor gives the service its cloud client: the first time, the built repo syncs."""
        if client_factory is None or self.vault_client_factory is not None:
            return
        self.vault_client_factory = client_factory
        if self.index is not None:
            self.sync_vault()

    def repo_profile(self) -> dict:
        # Snapshot under the lock, then compute everything from the
        # snapshot -- never re-touch the live dict, which apply_changes (run
        # off-loop by the watcher, F3) may be resizing concurrently.
        with self._lock:
            every = list((self.index or ProjectIndex()).files.values())
            embedded_chunks = len(self.vectors.ids()) if self.vectors else 0
            # the profile is the map of the CODE: a summary that leaves this process never names a document's
            # sections (security review 2026-11-04); other kinds are only counted
            files = [fi for fi in every if getattr(fi, "kind", "code") in ("code", "config")]
            vectors_ready = self.vectors_ready
            content_hashes = sorted(fi.content_hash for fi in files if fi.content_hash)
            endpoints = [e for fi in files for e in fi.endpoints]
            schemas = [x for fi in files for x in fi.schemas]
        langs = Counter(fi.lang for fi in files if fi.lang != "other")
        kinds = Counter(s.kind for fi in files for s in fi.symbols)
        top = [f"{s.name} ({s.kind}) @ {s.path}:{s.start_line}"
               for fi in files[:200] for s in fi.symbols][:_MAX_PROFILE_SAMPLE]
        hints = [h for h in _TEST_HINT_FILES if os.path.exists(os.path.join(self.root, h))]
        digest = hashlib.sha256("".join(content_hashes).encode()).hexdigest()
        endpoints.sort(key=lambda x: (x.get("route", ""), x.get("method", ""), x.get("path", "")))
        schemas.sort(key=lambda x: (x.get("name", ""), x.get("path", "")))
        return {
            "repo_key": self.repo_key,
            "evidence_version": 1,
            "content_digest": digest,
            "file_count": len(files),
            "other_kinds": dict(Counter(getattr(fi, "kind", "code") for fi in every
                                        if getattr(fi, "kind", "code") not in ("code", "config"))),
            "languages": dict(langs),
            "symbol_kinds": dict(kinds),
            "top_symbols": top,               # capped at _MAX_PROFILE_SAMPLE
            "test_hint_files": hints,
            # fresh = the index reflects the files on disk: built this session (or loaded for this
            # HEAD) and every change the watcher or the agent's own writes reported applied
            "index_fresh": self.index is not None and self.fresh,
            # why not: the watcher stopped (missing, refused by the OS, crashed) -- an edit may be missing
            **({"index_stale_reason": self.stale_reason} if self.index is not None and not self.fresh
               and self.stale_reason else {}),
            # the cloud code map: when this session last synced it, and why the last sync failed if it did
            "code_map": self.sync_state(),
            "git_ref": self.git_ref[:12],
            "embedded_chunks": embedded_chunks,
            "vectors_ready": vectors_ready,
            "endpoint_count": len(endpoints),
            "schema_count": len(schemas),
            # Account transport stays bounded; totals + cap metadata make a
            # large repo's projection explicitly partial rather than quietly
            # masquerading as the complete contract graph.
            "contract_evidence_limit": _MAX_CONTRACT_EVIDENCE,
            "contract_evidence_complete": (len(endpoints) <= _MAX_CONTRACT_EVIDENCE
                                           and len(schemas) <= _MAX_CONTRACT_EVIDENCE),
            "endpoints": endpoints[:_MAX_CONTRACT_EVIDENCE],
            "schemas": schemas[:_MAX_CONTRACT_EVIDENCE],
            # a folder of repositories is no one repository: the kernel names them instead of one mixed profile
            # (live 2026-10-25: "git:" empty, 20 symbols drawn from the whole folder)
            **({"repos": sub_repositories(self.root)} if not is_repository(self.root) else {}),
        }


# ── code graph everywhere (plan 3 Tasks 7, 9): the local workspace in the vault's one graph ─────────

# what makes a folder a project for the code graph: version control or a manifest of an indexable language at its root
# (the sdk's config names; Python and JS/TS are what the index reads)
_MANIFESTS = tuple(indexer.CONFIG_NAMES) + ("setup.py", "requirements.txt", "tsconfig.json")


SUB_REPOS_MAX = 40     # a folder of projects: at most this many of its repositories are synced


def sub_repositories(root: str) -> list:
    """The repositories directly inside a folder that is none itself: sub-folders with ``.git`` (never a link, never a
    hidden folder; a manifest alone does not make a sub-folder a project here), sorted, bounded."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(root, name)
        if name.startswith(".") or os.path.islink(full) or not os.path.isdir(full):
            continue
        try:
            if ".git" in os.listdir(full):
                out.append(name)
        except OSError:
            continue
    return out[:SUB_REPOS_MAX]


def is_repository(root: str) -> bool:
    """A repository the code graph may hold: ``.git`` (a dir, or a file in a worktree) or a project manifest at the
    root. A home folder or a folder of projects is none -- its repositories are synced one by one (owner 2026-10-25)."""
    try:
        names = set(os.listdir(root))
    except OSError:
        return False
    return ".git" in names or any(m in names for m in _MANIFESTS)


_MACHINE_ID_FILE = os.path.join(os.path.expanduser("~"), ".local", "share", "webbee", "machine-id")


def machine_id() -> str:
    """This machine's id for the code catalog (the vault separates two checkouts of one repo by it) -- ONE per machine
    whatever Python or process asks: /etc/machine-id; the hardware UUID on macOS (live 2026-10-24: Python 3.12 there
    returned a random uuid.getnode() per process, so every start registered a new machine); MachineGuid on Windows;
    a real network address (the id earlier releases sent); else an id made once and kept."""
    try:
        with open("/etc/machine-id", encoding="utf-8") as f:
            mid = f.read().strip()
            if mid:
                return mid
    except OSError:
        pass
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                               timeout=5)
            for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
                if "IOPlatformUUID" in line and "=" in line:
                    hw = line.split("=", 1)[1].strip().strip('"').replace("-", "").lower()
                    if hw:
                        return hw[:32]
        elif os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                guid = str(winreg.QueryValueEx(k, "MachineGuid")[0]).replace("-", "").lower()
                if guid:
                    return guid[:32]
    except Exception:
        pass
    node = uuid.getnode()
    if not (node >> 40) & 1:            # the multicast bit marks a random node: never the machine's id
        return hashlib.sha256(f"webbee|{node}".encode()).hexdigest()[:32]
    try:
        with open(_MACHINE_ID_FILE, encoding="utf-8") as f:
            kept = f.read().strip()
            if kept:
                return kept
    except OSError:
        pass
    made = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(_MACHINE_ID_FILE), exist_ok=True)
        with open(_MACHINE_ID_FILE, "w", encoding="utf-8") as f:
            f.write(made)
    except OSError:
        pass
    return made


def local_git_identity(root: str) -> dict:
    """``{remote, branch, commit}`` read from the repo's own .git files (no git binary needed)."""
    meta: dict = {}
    for rel in (".git/HEAD", ".git/config", ".git/packed-refs"):
        try:
            with open(os.path.join(root, rel), "rb") as f:
                meta[rel] = f.read(262144)
        except OSError:
            continue
    head = meta.get(".git/HEAD", b"").decode("utf-8", "replace")
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        try:
            with open(os.path.join(root, ".git", *ref.split("/")), "rb") as f:
                meta[f".git/{ref}"] = f.read(4096)
        except OSError:
            pass
    return indexer.git_identity(meta)


def co_change_pairs(root: str, commits: int = 500, max_files: int = 30) -> list:
    """Files changed together in the repo's own git history (``git log --name-only``, run on the LOCAL
    disk only -- never on a server): ``changed together`` links both ways, confidence from how often.
    No git binary or no history -> []."""
    try:
        r = subprocess.run(["git", "-C", root, "log", "--name-only", f"-n{int(commits)}", "--format=%x00"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    counts: dict = {}
    for block in r.stdout.split("\x00"):
        files = sorted({ln.strip() for ln in block.splitlines() if ln.strip() and indexer.indexable(ln.strip()) == "code"})
        if len(files) < 2 or len(files) > max_files:
            continue
        for i, a in enumerate(files):
            for b in files[i + 1:]:
                counts[(a, b)] = counts.get((a, b), 0) + 1
    out = []
    for (a, b), n in sorted(counts.items(), key=lambda kv: -kv[1])[:1000]:
        conf = round(min(1.0, 0.2 * n), 2)
        out += [{"src_path": a, "dst_repo": "", "dst_path": b, "confidence": conf, "origin": "changed together"},
                {"src_path": b, "dst_repo": "", "dst_path": a, "confidence": conf, "origin": "changed together"}]
    return out


async def vault_sync(client, meta: dict, listing: list, read, measured=None) -> dict:
    """One sync through the gateway's owner-scoped ``/v1/memory/code/sync/*`` -- the sdk's one sync
    algorithm (``code_index.sync_source``); the repo's ``changed together`` links ride its last apply."""
    async def plan(items, m):
        return await client._request("POST", "/v1/memory/code/sync/plan", json={"listing": items, "meta": m},
                                     timeout=120)

    async def apply(**payload):
        if measured and not (payload.get("meta") or {}).get("defer_links"):
            payload = {**payload, "measured": measured}
        return await client._request("POST", "/v1/memory/code/sync/apply", json=payload, timeout=300)
    return await indexer.sync_source(plan, apply, meta, listing, read)


# A listing of the whole workspace holds at most this many code files (only indexable files count -- images, docs and
# build output never spend it); past it the listing is sent as partial, never as complete.
LISTING_MAX = 20000


def _code_files(root: str) -> tuple[list[str], str]:
    """``(the workspace's code files, why the list is partial or "")``: in a git repository the files git tracks or
    would track (``git ls-files -co --exclude-standard`` -- a .gitignore'd file, generated code or a local config
    with keys, never reaches the cloud), else a walk; only indexable files count toward ``LISTING_MAX``."""
    paths: list[str] = []
    if os.path.isdir(os.path.join(root, ".git")):
        try:
            r = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
                               capture_output=True, timeout=60)
            if r.returncode == 0:
                paths = [p for p in r.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
        except (OSError, subprocess.SubprocessError):
            paths = []
    if not paths:
        paths = _walk(root, limit=10 * LISTING_MAX)
    code = [p for p in paths if indexer.indexable(p.replace(os.sep, "/")) and not ignored_path(root, p)]
    if len(code) > LISTING_MAX:
        return code[:LISTING_MAX], f"listing capped at {LISTING_MAX} code files"
    return code, ""


def _listing_sig(listing: list, capped: str = "") -> str:
    """PURE. One hash of a full listing (every file's path, size and time) -- equal listings, equal files."""
    h = hashlib.sha256(capped.encode())
    for it in sorted(listing, key=lambda x: x.get("path", "")):
        h.update(f"{it.get('path')}\0{it.get('size')}\0{it.get('mtime')}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _local_listing_of(root: str, rels=None) -> tuple[list, str]:
    """``(listing, why it is partial or "")`` -- the whole workspace's code files, or ``rels``."""
    if rels is not None:
        return _local_listing(root, rels), ""
    files, capped = _code_files(root)
    return _local_listing(root, files), capped


def _local_listing(root: str, rels=None) -> list:
    out = []
    for rel in (rels if rels is not None else _code_files(root)[0]):
        rel = rel.replace(os.sep, "/")
        if not indexer.indexable(rel) or ignored_path(root, rel):
            continue
        try:
            st = os.stat(os.path.join(root, rel))
            out.append({"path": rel, "size": int(st.st_size), "mtime": float(st.st_mtime)})
        except OSError:
            out.append({"path": rel, "size": -1, "mtime": 0.0})     # gone: removed from the vault
    return out
