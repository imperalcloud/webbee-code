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

# numpy is NOT imported at module scope: this module is imported eagerly by
# service.py, but numpy is declared only in the intel-embed* extras, not the
# base `intel` extra. save_vectors/load_vectors are the only functions that
# touch it -- import it locally there so an intel-only install (no embed
# extra) can still import this whole module (index.json save/load, the
# graph plane) with no numpy on the system at all.

SCHEMA_VERSION = 2


def _path(cache_dir: str, repo_key: str) -> str:
    return os.path.join(cache_dir, repo_key, "index.json")


def save(cache_dir: str, repo_key: str, index: ProjectIndex) -> None:
    p = _path(cache_dir, repo_key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = {"schema_version": SCHEMA_VERSION, "git_ref": index.git_ref,
            "files": {k: {"path": v.path, "lang": v.lang, "imports": v.imports, "refs": v.refs,
                          "symbols": [asdict(s) for s in v.symbols]} for k, v in index.files.items()}}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)  # atomic -- a reader never observes a half-written file


def load(cache_dir: str, repo_key: str, git_ref: str) -> ProjectIndex | None:
    # The WHOLE body (not just json.load) is guarded: a structurally-valid
    # but wrong-shape cache (missing/renamed field, unreadable dict) must
    # degrade to a cache miss, never raise -- raising here would propagate
    # out of build() and disable intel for the whole session instead of
    # falling through to a fresh re-index.
    try:
        with open(_path(cache_dir, repo_key), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("git_ref") != git_ref:
            return None  # stale cache -- the caller falls back to a full re-index
        if data.get("schema_version") != SCHEMA_VERSION:
            return None  # unknown/old on-disk shape -- clean miss, re-index
        idx = ProjectIndex(git_ref=data.get("git_ref", ""))
        for k, v in (data.get("files") or {}).items():
            idx.files[k] = FileIndex(path=v["path"], lang=v["lang"],
                                     imports=v.get("imports", []), refs=v.get("refs", []),
                                     symbols=[Symbol(**s) for s in v.get("symbols", [])])
        return idx
    except Exception:
        return None  # miss, corrupt, or wrong-shape -- never raise into the caller


def _vec_dir(cache_dir: str, repo_key: str) -> str:
    return os.path.join(cache_dir, repo_key)


def save_vectors(cache_dir, repo_key, git_ref, model_id, ids, matrix) -> None:
    # Data FIRST, manifest LAST: chunks.json is the commit point. A crash
    # between the two atomic os.replace() calls then always leaves the data
    # present but the manifest absent/stale -- load_vectors's
    # len(ids) != mat.shape[0] / schema check already treats that as a clean
    # miss (re-embed), never a manifest pointing at a missing/corrupt .npy.
    import numpy as np
    d = _vec_dir(cache_dir, repo_key)
    os.makedirs(d, exist_ok=True)
    tmp_n = os.path.join(d, "embeddings.npy.tmp")
    np.save(tmp_n, np.asarray(matrix, dtype=np.float32))
    # np.save appends ".npy" to a filename that doesn't already end with it --
    # tmp_n ends in ".tmp" so the file actually written is tmp_n + ".npy".
    # Replace from that real path so the final file lands as exactly
    # "embeddings.npy" with no "embeddings.npy.npy" artifact left behind.
    written = tmp_n if tmp_n.endswith(".npy") else tmp_n + ".npy"
    os.replace(written, os.path.join(d, "embeddings.npy"))
    meta = {"schema_version": SCHEMA_VERSION, "git_ref": git_ref, "model_id": model_id, "ids": list(ids)}
    tmp_j = os.path.join(d, "chunks.json.tmp")
    with open(tmp_j, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp_j, os.path.join(d, "chunks.json"))


def load_vectors(cache_dir, repo_key, git_ref, model_id):
    # Same never-raise contract as load(): any mismatch/corruption/missing
    # file degrades to a cache miss so callers fall back to a fresh embed.
    import numpy as np
    try:
        d = _vec_dir(cache_dir, repo_key)
        with open(os.path.join(d, "chunks.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        if (meta.get("schema_version") != SCHEMA_VERSION or meta.get("git_ref") != git_ref
                or meta.get("model_id") != model_id):
            return None
        mat = np.load(os.path.join(d, "embeddings.npy"))
        ids = meta.get("ids") or []
        if len(ids) != mat.shape[0]:
            return None
        return ids, mat.astype(np.float32)
    except Exception:
        return None
