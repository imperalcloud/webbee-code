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
