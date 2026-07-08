import pytest
pytest.importorskip("tree_sitter")  # skip when the intel extra isn't installed
from webbee.intel import indexer
from webbee.intel.models import ProjectIndex


def test_parse_python_symbols(tmp_path):
    src = "def alpha(x):\n    return beta(x)\n\nclass Gamma:\n    def method(self):\n        pass\n"
    fi = indexer.parse_file("m.py", src)
    names = {s.name for s in fi.symbols}
    assert {"alpha", "Gamma", "method"} <= names
    a = next(s for s in fi.symbols if s.name == "alpha")
    assert a.kind == "function" and a.start_line == 1


def test_parse_ts_symbols():
    fi = indexer.parse_file("m.ts", "export function widget(a: number){ return a }\nclass Box {}\n")
    names = {s.name for s in fi.symbols}
    assert "widget" in names and "Box" in names


def test_unsupported_ext_is_line_only_not_crash():
    fi = indexer.parse_file("data.bin", "\x00\x01binary")
    assert fi is not None and fi.symbols == [] and fi.lang == "other"


def test_build_index(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    return b()\n")
    (tmp_path / "b.py").write_text("def b():\n    return 1\n")
    idx = indexer.build_index(str(tmp_path), ["a.py", "b.py"])
    assert isinstance(idx, ProjectIndex)
    assert set(idx.files) == {"a.py", "b.py"}
    assert any(s.name == "a" for s in idx.files["a.py"].symbols)
