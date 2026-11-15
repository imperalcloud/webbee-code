import json
import os

import numpy as np
import pytest
pytest.importorskip("tree_sitter")
from webbee.intel import store
from imperal_sdk import code_index as indexer


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




def test_one_cache_folder_per_repository(tmp_path, monkeypatch):
    """Owner 2026-11-15 (phase 6 decision 3): a subfolder's index is a file in its repository's folder."""
    import subprocess
    repo = tmp_path / "repo"
    (repo / "pkg" / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    top, sub = store.cache_key(str(repo)), store.cache_key(str(repo / "pkg" / "sub"))
    assert "/" not in top and sub.startswith(top + "/") and sub != store.cache_key(str(repo / "pkg"))
    (tmp_path / "a.py").write_text("def a():\n    pass\n")
    idx = indexer.build_index(str(tmp_path), ["a.py"]); idx.git_ref = "abc"
    cache = str(tmp_path / "cache")
    store.save(cache, top, idx)
    store.save(cache, sub, idx)
    assert sorted(os.listdir(cache)) == [top]
    assert sorted(os.listdir(os.path.join(cache, top))) == ["index.json", f"sub-{sub.split('/')[1]}.json"]
    assert store.load(cache, sub, "abc") is not None


def test_the_old_layout_is_removed_once_and_a_set_folder_is_never_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / ".cache" / "webbee" / "intel" / "0123456789ab"
    old.mkdir(parents=True)
    (old / "index.json").write_text("{}")
    monkeypatch.setattr(store, "_DROPPED", False)
    assert store.default_dir() == str(tmp_path / "webbee-index-cache")     # a set folder: the old one stays
    assert old.exists()
    monkeypatch.delenv("IMPERAL_INTEL_CACHE_DIR")
    assert store.default_dir() == str(tmp_path / ".cache" / "webbee" / "index")
    assert not (tmp_path / ".cache" / "webbee" / "intel").exists()
