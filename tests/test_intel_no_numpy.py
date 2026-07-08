"""F2 regression: `pip install webbee[intel]` (no embed extra -- no numpy,
no model2vec) must leave intel FUNCTIONAL as lexical+graph, never crash
build() or degrade search_code to an error envelope. Simulated here by
forcing embed.load_backend() to raise (standing in for the real failure
mode: a bare `import numpy` inside embed.py/store.py with numpy absent)."""
import pytest
pytest.importorskip("tree_sitter")
from webbee.intel.service import IntelService
from webbee.intel import query, embed as embed_mod


def test_intel_only_degrades_to_lexical_graph_when_embed_import_fails(tmp_path, monkeypatch):
    def _raise():
        raise ImportError("no module named 'model2vec'")  # stands in for a numpy-less install
    monkeypatch.setattr(embed_mod, "load_backend", _raise)

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk_no_numpy", cache_dir=str(tmp_path / "c"))
    svc.build()  # must not raise -- a missing embed backend is fail-soft, not fatal

    assert svc.vectors is None
    assert svc.vectors_ready is False

    r = query.search_code(svc, "alpha")
    assert r["ok"] is True                                    # well-formed envelope, not an error
    assert any(i["title"] == "alpha" for i in r["data"]["items"])  # lexical+graph still answers


def test_get_backend_caches_none_after_import_failure(tmp_path, monkeypatch):
    # The None result must be cached (sentinel discipline) -- a broken
    # backend shouldn't be retried on every apply_changes/search_code call.
    calls = {"n": 0}

    def _raise():
        calls["n"] += 1
        raise ImportError("boom")
    monkeypatch.setattr(embed_mod, "load_backend", _raise)

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk_no_numpy2", cache_dir=str(tmp_path / "c"))
    svc.build()
    assert svc._get_backend() is None
    assert calls["n"] == 1  # not called again -- cached
