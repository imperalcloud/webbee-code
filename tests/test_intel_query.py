import pytest
pytest.importorskip("tree_sitter")
from webbee.intel.service import IntelService
from webbee.intel import query


def _svc(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return beta()\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 1\n")
    s = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c")); s.build(); return s


def test_graph_slice_envelope(tmp_path):
    r = query.graph_slice(_svc(tmp_path), ["beta"])
    assert r["ok"] and "data" in r
    items = r["data"]["items"]
    assert r["data"]["total"] == len(items)
    b = next(i for i in items if i["title"] == "beta")
    assert b["id"] == "b.py::beta" and b["kind"] == "function"


def test_search_code_envelope(tmp_path):
    r = query.search_code(_svc(tmp_path), "beta")
    assert r["ok"] and all({"id", "title", "kind"} <= set(i) for i in r["data"]["items"])


def test_impact_of_change(tmp_path):
    r = query.impact_of_change(_svc(tmp_path), ["beta"])
    # a.py references beta -> a.py is impacted
    assert any(i["id"].startswith("a.py") for i in r["data"]["items"])


def test_repo_profile_envelope(tmp_path):
    r = query.repo_profile(_svc(tmp_path))
    assert r["ok"] and r["data"]["items"][0]["kind"] == "repo"


def test_orient_bundle(tmp_path):
    r = query.orient(_svc(tmp_path), "beta")
    assert r["ok"] and r["data"]["items"]  # profile + slices + hits merged


def test_graph_slice_and_impact_of_change_coerce_bare_string_symbols(tmp_path):
    # Belt-and-suspenders: even called directly with a bare string (not via
    # the tools.py _cpc coercion), these must not char-iterate the string.
    svc = _svc(tmp_path)
    r = query.graph_slice(svc, "beta")
    assert r["ok"] and any(i["title"] == "beta" for i in r["data"]["items"])
    r2 = query.impact_of_change(svc, "beta")
    assert r2["ok"] and any(i["id"].startswith("a.py") for i in r2["data"]["items"])


def test_rrf_fusion_pure():
    from webbee.intel.query import _fuse_rrf
    fused = _fuse_rrf([["a", "b", "c"], ["b", "d"]], k=60)  # b appears in both -> top
    assert fused[0] == "b"


def test_search_code_falls_back_without_vectors(tmp_path):
    svc = _svc(tmp_path)
    svc.vectors = None; svc.vectors_ready = False       # force the no-vector path
    r = query.search_code(svc, "beta")
    assert r["ok"] and any(i["title"] == "beta" for i in r["data"]["items"])   # lexical still answers
