"""IntelService -- owns the index + graph + freshness for one repo. This is
the object injected everywhere (executor/session/repl): `.build()` runs the
cold path (cache-load or full index) off the event loop, `.apply_changes()`
does the cheap incremental re-parse the watcher drives, and `.repo_profile()`
is the bounded summary handed to the brain."""
from __future__ import annotations
import os
import subprocess
import threading
from collections import Counter

from webbee.intel import indexer, store
from webbee.intel.graph import CodeGraph
from webbee.intel.models import ProjectIndex

_MAX_PROFILE_SAMPLE = 20
_TEST_HINT_FILES = ("pytest.ini", "tox.ini", "package.json", "Makefile", "pyproject.toml")


def _git_ref(root: str) -> str:
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _walk(root: str, limit: int = 20000) -> list[str]:
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != ".git" and not d.startswith(".") and d != "node_modules"]
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
        self.vectors: VectorStore | None = None
        self.vectors_ready = False
        self._backend = "__unset__"          # sentinel -- distinct from a real None result
        self._chunk_hashes: dict[str, str] = {}  # chunk id -> content_hash, for incremental skip
        # Guards the index/graph mutation (apply_changes, driven by the
        # watcher off-loop via asyncio.to_thread) against a concurrent
        # repo_profile() read -- without it, a worker thread can resize
        # self.index.files mid-iteration on the reader's side.
        self._lock = threading.Lock()

    def _get_backend(self):
        # Lazy + cached: the model load is expensive (and may be unavailable
        # entirely), so it happens at most once per service instance. A
        # None result is cached too -- unavailable stays unavailable rather
        # than retrying the load on every call.
        if self._backend == "__unset__":
            from webbee.intel import embed
            self._backend = embed.load_backend()
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
        self._embed_build(idx)

    def _embed_build(self, idx: ProjectIndex) -> None:
        # Off-loop already (build() is called via asyncio.to_thread from the
        # repl boot): cache-load skips re-embedding entirely when the
        # git_ref+model_id match; a miss chunks+embeds+saves. Fail-soft --
        # no backend means self.vectors stays None and search_code (F5)
        # falls back to lexical+graph instead of raising.
        from webbee.intel import chunker, store as _store
        from webbee.intel.vectors import VectorStore
        b = self._get_backend()
        if b is None:
            self.vectors = None
            self.vectors_ready = False
            return
        cached = _store.load_vectors(self.cache_dir, self.repo_key, self.git_ref, b.model_id)
        if cached is not None:
            ids, mat = cached
            vs = VectorStore.from_arrays(b.dim, ids, mat)
        else:
            chunks = chunker.chunk_index(self.root, idx)
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
        if embed_ready:
            from webbee.intel import chunker
            b = self._get_backend()
            fresh_chunks = []
            for rel, fi in updates.items():
                if fi is None:
                    continue  # deleted -- handled via vs.remove() below
                for c in chunker.chunk_file(self.root, fi):
                    if self._chunk_hashes.get(c.id) != c.content_hash:
                        fresh_chunks.append(c)
            if fresh_chunks:
                fresh_vecs = b.embed([c.text for c in fresh_chunks])
                fresh_ids = [c.id for c in fresh_chunks]
                for c in fresh_chunks:
                    self._chunk_hashes[c.id] = c.content_hash

        with self._lock:
            for rel, fi in updates.items():
                if fi is None:
                    self.index.files.pop(rel, None)
                else:
                    self.index.files[rel] = fi
            self.graph = CodeGraph(self.index)
            if embed_ready:
                deleted = [rel for rel, fi in updates.items() if fi is None]
                if deleted:
                    self.vectors.remove([i for i in self.vectors.ids()
                                          if any(i.startswith(f"{rel}#") for rel in deleted)])
                if fresh_ids:
                    self.vectors.add(fresh_ids, fresh_vecs)

    def repo_profile(self) -> dict:
        # Snapshot under the lock, then compute everything from the
        # snapshot -- never re-touch the live dict, which apply_changes (run
        # off-loop by the watcher, F3) may be resizing concurrently.
        with self._lock:
            files = list((self.index or ProjectIndex()).files.values())
            embedded_chunks = len(self.vectors.ids()) if self.vectors else 0
            vectors_ready = self.vectors_ready
        langs = Counter(fi.lang for fi in files if fi.lang != "other")
        kinds = Counter(s.kind for fi in files for s in fi.symbols)
        top = [f"{s.name} ({s.kind}) @ {s.path}:{s.start_line}"
               for fi in files[:200] for s in fi.symbols][:_MAX_PROFILE_SAMPLE]
        hints = [h for h in _TEST_HINT_FILES if os.path.exists(os.path.join(self.root, h))]
        return {
            "repo_key": self.repo_key,
            "file_count": len(files),
            "languages": dict(langs),
            "symbol_kinds": dict(kinds),
            "top_symbols": top,               # capped at _MAX_PROFILE_SAMPLE
            "test_hint_files": hints,
            "index_fresh": bool(self.git_ref),
            "git_ref": self.git_ref[:12],
            "embedded_chunks": embedded_chunks,
            "vectors_ready": vectors_ready,
        }
