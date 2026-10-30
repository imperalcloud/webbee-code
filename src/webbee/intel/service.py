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

# The workspace reaches the cloud code map at least this often while the session works (2026-10-30: a repo of a
# user's sessions was last synced 5 days before, a folder's map 5 days too -- the client synced at start, on the
# watcher's changes and when the kernel asked before a code question; a shell edit, a git pull, a failed start sync
# left the map old without a word). A full listing: the vault's plan sends only what changed.
SYNC_DUE_S = 300

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
    return name.startswith(".") or name in _SKIP_DIRS or _is_virtualenv(os.path.join(parent, name))


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


def _walk(root: str, limit: int = 20000) -> list[str]:
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not skipped_dir(dp, d)]
        for fn in fns:
            out.append(os.path.relpath(os.path.join(dp, fn), root))
            if len(out) >= limit:
                return out
    return out


class IntelService:
    def __init__(self, root: str, repo_key: str, cache_dir: str = "") -> None:
        self.root = os.path.realpath(root)
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
        self.last_sync: dict = {}
        self.last_sync_ok_at = 0.0         # the last sync the vault took (time.time()); 0 -- none this session
        self.last_sync_error = ""          # why the last sync failed; "" -- it did not

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
        call via asyncio.to_thread from the repl boot."""
        self.git_ref = _git_ref(self.root)
        idx = store.load(self.cache_dir, self.repo_key, self.git_ref) if self.git_ref else None
        if idx is None:
            idx = indexer.build_index(self.root, _walk(self.root))
            idx.git_ref = self.git_ref
            if self.git_ref:
                try:
                    store.save(self.cache_dir, self.repo_key, idx)
                except OSError:
                    pass
        self.index = idx
        self.graph = CodeGraph(idx)
        self.fresh = True
        self._embed_build(idx)
        self.sync_vault()                 # code graph everywhere: the whole repo into the vault

    def _embed_build(self, idx: ProjectIndex) -> None:
        # Off-loop already (build() is called via asyncio.to_thread from the
        # repl boot): cache-load skips re-embedding entirely when the
        # git_ref+model_id match; a miss chunks+embeds+saves. Fail-soft --
        # no backend means self.vectors stays None and search_code (F5)
        # falls back to lexical+graph instead of raising.
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

    def apply_changes(self, rel_paths) -> None:
        # Parse off-lock (file I/O + tree-sitter parse don't touch shared
        # state), then apply the mutation + graph rebuild atomically under
        # the lock -- a concurrent repo_profile() read must never observe a
        # partially-updated index.
        if self.index is None:
            return
        updates = {}
        for rel in rel_paths:
            ap = os.path.join(self.root, rel)
            if not os.path.exists(ap):
                updates[rel] = None  # deleted -> drop from the index
                continue
            try:
                with open(ap, "r", encoding="utf-8") as f:
                    fi = indexer.parse_file(rel, f.read())
            except (OSError, UnicodeDecodeError):
                continue
            if fi is not None:
                updates[rel] = fi

        # Embed compute (chunk + backend inference) stays off-lock, mirroring
        # the parse-then-swap pattern above -- only the vector-store mutation
        # happens under `_lock`. Skipped entirely when there's no live vector
        # store or the backend is unavailable (fail-soft, F5's fallback).
        embed_ready = self.vectors is not None and self._get_backend() is not None
        fresh_ids: list = []
        fresh_vecs = None
        current_ids: dict[str, set] = {}   # edited rel -> its FULL current chunk-id set
        if embed_ready:
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

        self.sync_vault(list(updates))    # only the changed files go to the vault
        with self._lock:
            for rel, fi in updates.items():
                if fi is None:
                    self.index.files.pop(rel, None)
                else:
                    self.index.files[rel] = fi
            self.graph = CodeGraph(self.index)
            if embed_ready:
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
                measured = co_change_pairs(root) if rels is None and history else None
                out = asyncio.run(vault_sync(factory(), meta, listing, read, measured))
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


    def sync_now(self) -> dict:
        """The kernel asks before a code question (frame phase A): the workspace reaches the vault now -- a full
        listing, so an edit no watcher reported (a shell edit) is sent too; the vault's plan picks the changed files.
        The git history is not re-read. ``{repo_key, synced_at, ...counts}`` or ``{ok: False, content}``."""
        if self.vault_client_factory is None:
            return {"ok": False, "content": "this session cannot reach the cloud"}
        if not self.last_sync and not self._sync_lock.acquire(blocking=False):
            # the repo's first sync (a whole repo: extract + embed) is still running: never wait behind it
            return {"ok": False, "content": "the first code sync of this repo is still running"}
        if not self.last_sync:
            self._sync_lock.release()
        out = self._sync_vault(None, history=False)
        if not out.get("repo_key") and not out.get("repos"):
            return {"ok": False, "content": str(out.get("partial_reason") or self.last_sync_error
                                                or "the code sync to the vault failed")}
        return {**out, "synced_at": time.time()}

    def sync_vault(self, rels=None, history: bool = True) -> None:
        """Off the caller's thread: the build and the watcher never wait for the cloud."""
        if self.vault_client_factory is None:
            return
        threading.Thread(target=self._sync_vault, args=(rels, history), name="webbee-code-sync",
                         daemon=True).start()

    def sync_if_due(self, now: float | None = None) -> bool:
        """A full sync of the workspace off the caller's thread when the last one the vault took is older than
        ``SYNC_DUE_S`` (or none took) -- called on every tool call, so a working session's map is never older than
        that; never a second sync beside a running one. True when one started."""
        if self.vault_client_factory is None or self.index is None:
            return False
        if (now if now is not None else time.time()) - self.last_sync_ok_at < SYNC_DUE_S:
            return False
        if self._sync_lock.locked():
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
            files = list((self.index or ProjectIndex()).files.values())
            embedded_chunks = len(self.vectors.ids()) if self.vectors else 0
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
            "languages": dict(langs),
            "symbol_kinds": dict(kinds),
            "top_symbols": top,               # capped at _MAX_PROFILE_SAMPLE
            "test_hint_files": hints,
            # fresh = the index reflects the files on disk: built this session (or loaded for this
            # HEAD) and every change the watcher or the agent's own writes reported applied
            "index_fresh": self.index is not None and self.fresh,
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
