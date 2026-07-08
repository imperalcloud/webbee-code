"""IntelService -- owns the index + graph + freshness for one repo. This is
the object injected everywhere (executor/session/repl): `.build()` runs the
cold path (cache-load or full index) off the event loop, `.apply_changes()`
does the cheap incremental re-parse the watcher drives, and `.repo_profile()`
is the bounded summary handed to the brain."""
from __future__ import annotations
import os
import subprocess
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

    def apply_changes(self, rel_paths) -> None:
        if self.index is None:
            return
        for rel in rel_paths:
            ap = os.path.join(self.root, rel)
            if not os.path.exists(ap):
                self.index.files.pop(rel, None); continue
            try:
                with open(ap, "r", encoding="utf-8") as f:
                    fi = indexer.parse_file(rel, f.read())
                if fi is not None:
                    self.index.files[rel] = fi
            except (OSError, UnicodeDecodeError):
                continue
        self.graph = CodeGraph(self.index)

    def repo_profile(self) -> dict:
        idx = self.index or ProjectIndex()
        langs = Counter(fi.lang for fi in idx.files.values() if fi.lang != "other")
        kinds = Counter(s.kind for fi in idx.files.values() for s in fi.symbols)
        top = [f"{s.name} ({s.kind}) @ {s.path}:{s.start_line}"
               for fi in list(idx.files.values())[:200] for s in fi.symbols][:_MAX_PROFILE_SAMPLE]
        hints = [h for h in _TEST_HINT_FILES if os.path.exists(os.path.join(self.root, h))]
        return {
            "repo_key": self.repo_key,
            "file_count": len(idx.files),
            "languages": dict(langs),
            "symbol_kinds": dict(kinds),
            "top_symbols": top,               # capped at _MAX_PROFILE_SAMPLE
            "test_hint_files": hints,
            "index_fresh": bool(self.git_ref),
            "git_ref": self.git_ref[:12],
        }
