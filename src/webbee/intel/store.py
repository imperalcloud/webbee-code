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

from webbee.intel.models import ProjectIndex, FileIndex, Symbol


def _path(cache_dir: str, repo_key: str) -> str:
    return os.path.join(cache_dir, repo_key, "index.json")


def save(cache_dir: str, repo_key: str, index: ProjectIndex) -> None:
    p = _path(cache_dir, repo_key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = {"git_ref": index.git_ref,
            "files": {k: {"path": v.path, "lang": v.lang, "imports": v.imports, "refs": v.refs,
                          "symbols": [asdict(s) for s in v.symbols]} for k, v in index.files.items()}}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)  # atomic -- a reader never observes a half-written file


def load(cache_dir: str, repo_key: str, git_ref: str) -> ProjectIndex | None:
    try:
        with open(_path(cache_dir, repo_key), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None  # miss or corrupt -- never raise into the caller
    if data.get("git_ref") != git_ref:
        return None  # stale cache -- the caller falls back to a full re-index
    idx = ProjectIndex(git_ref=data.get("git_ref", ""))
    for k, v in (data.get("files") or {}).items():
        idx.files[k] = FileIndex(path=v["path"], lang=v["lang"],
                                 imports=v.get("imports", []), refs=v.get("refs", []),
                                 symbols=[Symbol(**s) for s in v.get("symbols", [])])
    return idx
