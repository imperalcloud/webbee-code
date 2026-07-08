from webbee.intel import query
import inspect


def test_search_code_signature_frozen():
    sig = inspect.signature(query.search_code)
    assert list(sig.parameters) == ["svc", "q", "k", "kind", "path_glob"]


def test_envelope_keys_frozen(tmp_path):
    import pytest
    pytest.importorskip("tree_sitter")
    from webbee.intel.service import IntelService
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.build()
    r = query.search_code(svc, "alpha")
    assert set(r) == {"ok", "data"} and set(r["data"]) == {"items", "total", "has_more"}
    if r["data"]["items"]:
        assert {"id", "title", "kind"} <= set(r["data"]["items"][0])
