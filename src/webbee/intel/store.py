"""On-disk index cache. JSON ONLY -- never pickle. The AGPL reference this
package's design draws from used `pickle.load` to read a per-repo cache,
which is arbitrary code execution the moment that cache directory (or a
`git clone` of an untrusted repo whose checkout seeds it) is attacker-
controlled. A JSON cache can only ever produce dicts/lists/strings/numbers,
so a poisoned or corrupted file degrades to a cache miss, never RCE."""
from __future__ import annotations
import json
import os
from dataclasses import asdict

from imperal_sdk.code_index import ProjectIndex, FileIndex, Symbol

# 3 (2026-07-25): chunk ids gained a ":{kind}" suffix (chunker._mk) to fix a
# collision that silently disabled code intel. Every cached `ids` list from
# schema 2 holds the OLD id format, so the index cache must be treated as a
# clean miss and rebuilt -- bumping the version is what makes load() do that.
# 5 (2026-11-04): a file's ``kind`` (code | config | document | data | mail | image) is kept -- a schema-4 cache
# read every document back as code after a restart; a folder without git is cached too (``NO_GIT``) and the
# checksum tree of the folder (``tree``) is saved with the stats.
# 6 (2026-11-13): symbols carry their doc; nested arrow functions are symbols (imperal-sdk EXTRACTOR_VERSION 5)
SCHEMA_VERSION = 6
NO_GIT = "-"           # the cache key of a folder that is no git repository (its stats keep it honest)


# owner 2026-11-15 (phase 6 decision 3): ONE folder per repository. Before, every folder an index was asked for (a
# subfolder ``root=``, a unit of a folder of repositories) got its own folder keyed by its own path: 1692 folders in
# one user's cache. Now a repository's folder holds its index and the index of each of its subfolders as a file; the
# old layout (``intel/``) is removed once and each repository is indexed again once.
DEFAULT_DIR = os.path.join("~", ".cache", "webbee", "index")
OLD_DIRS = (os.path.join("~", ".cache", "webbee", "intel"),)
_DROPPED = False


def default_dir() -> str:
    """Where the index cache lives: ``IMPERAL_INTEL_CACHE_DIR`` (tests, a user's own choice), else ``DEFAULT_DIR`` --
    whose first use in a process removes the old layout."""
    own = os.environ.get("IMPERAL_INTEL_CACHE_DIR", "")
    if own:
        return own
    drop_old_layout()
    return os.path.expanduser(DEFAULT_DIR)


def drop_old_layout() -> None:
    """The cache folders of the layout before one-folder-per-repository, removed once a process (a cache: rebuilt)."""
    global _DROPPED
    if _DROPPED:
        return
    _DROPPED = True
    import shutil
    for d in OLD_DIRS:
        shutil.rmtree(os.path.expanduser(d), ignore_errors=True)


def cache_key(root: str) -> str:
    """The cache key of a folder: its repository's key; a folder inside a repository adds a hash of its path there
    (``<repo key>/<12 hex>``) -- a file in the repository's folder, never a folder of its own."""
    import hashlib
    from webbee.repo import compute_repo_key, find_repo_root
    real = os.path.realpath(root)
    top = find_repo_root(real)
    key = compute_repo_key(top)
    if top == real:
        return key
    rel = os.path.relpath(real, top).replace(os.sep, "/")
    return key + "/" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


def _path(cache_dir: str, key: str) -> str:
    repo, _, sub = str(key).partition("/")
    return os.path.join(cache_dir, repo, f"sub-{sub}.json" if sub else "index.json")


def save(cache_dir: str, repo_key: str, index: ProjectIndex, stats: dict | None = None,
         tree: dict | None = None) -> None:
    """``stats`` -- ``{relpath: [mtime_ns, size]}`` of every file the walk saw when the index was taken: the next
    start re-parses only the files whose stat differs (edits made while Webbee was closed, uncommitted edits)."""
    p = _path(cache_dir, repo_key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = {"schema_version": SCHEMA_VERSION, "git_ref": index.git_ref or NO_GIT, "stats": stats or {},
            "tree": tree or {},
            "files": {k: {"path": v.path, "lang": v.lang, "kind": getattr(v, "kind", "code"), "imports": v.imports,
                          "refs": v.refs,
                          "content_hash": v.content_hash, "endpoints": v.endpoints, "schemas": v.schemas,
                          "symbols": [asdict(s) for s in v.symbols]} for k, v in index.files.items()}}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)  # atomic -- a reader never observes a half-written file


def load(cache_dir: str, repo_key: str, git_ref: str) -> ProjectIndex | None:
    got = load_with_stats(cache_dir, repo_key, git_ref)
    return got[0] if got is not None else None


def load_tree(cache_dir: str, repo_key: str) -> dict:
    """The checksum tree saved with the index (``{dir: hash}``), or ``{}``."""
    try:
        with open(_path(cache_dir, repo_key), "r", encoding="utf-8") as f:
            tree = json.load(f).get("tree") or {}
        return {str(k): str(v) for k, v in tree.items()} if isinstance(tree, dict) else {}
    except Exception:
        return {}


def load_with_stats(cache_dir: str, repo_key: str, git_ref: str) -> tuple[ProjectIndex, dict] | None:
    """``(index, {relpath: [mtime_ns, size]})`` saved for this HEAD, or None. A cache from before the stats were kept
    gives ``{}``: every file then counts as changed and is re-parsed once."""
    # The WHOLE body (not just json.load) is guarded: a structurally-valid
    # but wrong-shape cache (missing/renamed field, unreadable dict) must
    # degrade to a cache miss, never raise -- raising here would propagate
    # out of build() and disable intel for the whole session instead of
    # falling through to a fresh re-index.
    try:
        with open(_path(cache_dir, repo_key), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("git_ref") != (git_ref or NO_GIT):
            return None  # stale cache -- the caller falls back to a full re-index
        if data.get("schema_version") != SCHEMA_VERSION:
            return None  # unknown/old on-disk shape -- clean miss, re-index
        idx = ProjectIndex(git_ref=git_ref or "")
        for k, v in (data.get("files") or {}).items():
            idx.files[k] = FileIndex(path=v["path"], lang=v["lang"],
                                     imports=v.get("imports", []), refs=v.get("refs", []),
                                     content_hash=v.get("content_hash", ""), endpoints=v.get("endpoints", []),
                                     schemas=v.get("schemas", []), symbols=[Symbol(**s) for s in v.get("symbols", [])],
                                     kind=v.get("kind") or "code")
        stats = {k: list(v) for k, v in (data.get("stats") or {}).items()
                 if isinstance(v, list) and len(v) == 2}
        return idx, stats
    except Exception:
        return None  # miss, corrupt, or wrong-shape -- never raise into the caller
