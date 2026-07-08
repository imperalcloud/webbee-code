import json
import os

import numpy as np
import pytest
pytest.importorskip("tree_sitter")
from webbee.intel import store, indexer


def test_roundtrip(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    pass\n")
    idx = indexer.build_index(str(tmp_path), ["a.py"]); idx.git_ref = "abc"
    cache = str(tmp_path / "cache")
    store.save(cache, "rk1", idx)
    got = store.load(cache, "rk1", "abc")
    assert got is not None and "a.py" in got.files
    assert store.load(cache, "rk1", "different") is None   # ref mismatch -> miss
    assert store.load(cache, "rk_absent", "abc") is None


def test_corrupt_cache_file_is_a_miss_not_a_crash(tmp_path):
    # store.py must use JSON (never pickle -- pickle.load on an untrusted
    # checkout's cache is RCE). A truncated/garbage JSON file must degrade to
    # a cache miss, never raise.
    cache = str(tmp_path / "cache")
    p = tmp_path / "cache" / "rk2" / "index.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not valid json")
    assert store.load(cache, "rk2", "abc") is None


def _write_raw(tmp_path, repo_key: str, data) -> str:
    p = tmp_path / "cache" / repo_key / "index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return str(tmp_path / "cache")


def test_valid_json_null_is_a_miss_not_a_crash(tmp_path):
    # F4: structurally-valid JSON that isn't a dict (e.g. a bare `null`) must
    # degrade to a cache miss, not raise out of load() and disable intel.
    cache = _write_raw(tmp_path, "rk3", None)
    assert store.load(cache, "rk3", "abc") is None


def test_symbol_with_extra_or_renamed_key_is_a_miss_not_a_crash(tmp_path):
    # A structurally-valid-but-wrong-shape cache (e.g. after a Symbol field
    # rename) must not raise a TypeError out of Symbol(**s).
    data = {"git_ref": "abc",
            "files": {"a.py": {"path": "a.py", "lang": "python", "imports": [], "refs": [],
                                "symbols": [{"name": "a", "kind": "function", "path": "a.py",
                                             "start_line": 1, "end_line": 2, "signature": "",
                                             "extra_renamed_field": "x"}]}}}
    cache = _write_raw(tmp_path, "rk4", data)
    assert store.load(cache, "rk4", "abc") is None


def test_file_entry_missing_path_is_a_miss_not_a_crash(tmp_path):
    # A missing required key in a file entry must not raise a KeyError out
    # of the FileIndex reconstruction.
    data = {"git_ref": "abc",
            "files": {"a.py": {"lang": "python", "imports": [], "refs": [], "symbols": []}}}
    cache = _write_raw(tmp_path, "rk5", data)
    assert store.load(cache, "rk5", "abc") is None


def test_schema_version_mismatch_is_a_clean_miss(tmp_path):
    # A future/older on-disk schema_version must be treated as a clean miss
    # so build() re-indexes instead of trying to reconstruct a shape it
    # doesn't understand.
    (tmp_path / "a.py").write_text("def a():\n    pass\n")
    idx = indexer.build_index(str(tmp_path), ["a.py"]); idx.git_ref = "abc"
    cache = str(tmp_path / "cache")
    store.save(cache, "rk6", idx)
    p = tmp_path / "cache" / "rk6" / "index.json"
    data = json.loads(p.read_text())
    data["schema_version"] = 999
    p.write_text(json.dumps(data))
    assert store.load(cache, "rk6", "abc") is None


def test_save_vectors_writes_data_before_manifest(tmp_path, monkeypatch):
    # F3: a crash between the two atomic os.replace() calls must never leave
    # a manifest (chunks.json) pointing at an absent/stale embeddings.npy.
    # Writing the data FIRST and the manifest LAST makes chunks.json the
    # commit point -- a half-write always looks like "data present, manifest
    # absent/old", which load_vectors already treats as a clean miss.
    calls = []
    real_replace = os.replace

    def _tracking_replace(src, dst):
        calls.append(os.path.basename(dst))
        return real_replace(src, dst)
    monkeypatch.setattr(store.os, "replace", _tracking_replace)

    c = str(tmp_path / "c")
    ids = ["a.py#1-2"]; mat = np.array([[0.1, 0.2, 0.3]], dtype=np.float32)
    store.save_vectors(c, "rk", "gitA", "model2vec:potion", ids, mat)

    assert calls == ["embeddings.npy", "chunks.json"]


def test_vectors_roundtrip_gated(tmp_path):
    c = str(tmp_path / "c")
    ids = ["a.py#1-2"]; mat = np.array([[0.1, 0.2, 0.3]], dtype=np.float32)
    store.save_vectors(c, "rk", "gitA", "model2vec:potion", ids, mat)
    npy = tmp_path / "c" / "rk" / "embeddings.npy"
    assert npy.exists() and not (tmp_path / "c" / "rk" / "embeddings.npy.npy").exists()
    got = store.load_vectors(c, "rk", "gitA", "model2vec:potion")
    assert got is not None and got[0] == ids and got[1].shape == (1, 3)
    assert store.load_vectors(c, "rk", "gitB", "model2vec:potion") is None   # git mismatch
    assert store.load_vectors(c, "rk", "gitA", "other-model") is None        # model mismatch
    assert store.load_vectors(c, "rk_absent", "gitA", "model2vec:potion") is None
