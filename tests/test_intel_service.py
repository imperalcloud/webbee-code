import pytest
pytest.importorskip("tree_sitter")
from webbee.intel.service import IntelService


def test_build_and_profile(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.ts").write_text("export function beta(){}\n")
    svc = IntelService(str(tmp_path), "rk1", cache_dir=str(tmp_path / "c"))
    svc.build()
    assert svc.ready and svc.graph is not None
    prof = svc.repo_profile()
    assert prof["file_count"] >= 2
    assert "python" in prof["languages"] and "typescript" in prof["languages"]
    # bounded: entry-point/sample lists are capped
    assert isinstance(prof.get("top_symbols"), list) and len(prof["top_symbols"]) <= 20


def test_not_ready_before_build(tmp_path):
    svc = IntelService(str(tmp_path), "rk2", cache_dir=str(tmp_path / "c"))
    assert svc.ready is False
    assert svc.graph is None and svc.index is None


def test_apply_changes_incrementally_reparses_changed_file(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk3", cache_dir=str(tmp_path / "c"))
    svc.build()
    names_before = {s.name for fi in svc.index.files.values() for s in fi.symbols}
    assert "alpha" in names_before

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\ndef beta():\n    return 2\n")
    svc.apply_changes({"a.py"})
    names_after = {s.name for fi in svc.index.files.values() for s in fi.symbols}
    assert "beta" in names_after


def test_apply_changes_drops_deleted_file(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk4", cache_dir=str(tmp_path / "c"))
    svc.build()
    (tmp_path / "a.py").unlink()
    svc.apply_changes({"a.py"})
    assert "a.py" not in svc.index.files
