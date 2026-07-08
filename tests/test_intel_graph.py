from webbee.intel.models import ProjectIndex, FileIndex, Symbol
from webbee.intel.graph import CodeGraph


def _mk():
    idx = ProjectIndex()
    idx.files["a.py"] = FileIndex(path="a.py", lang="python",
        symbols=[Symbol("a", "function", "a.py", 1, 2)], refs=["b"])
    idx.files["b.py"] = FileIndex(path="b.py", lang="python",
        symbols=[Symbol("b", "function", "b.py", 1, 2)], refs=[])
    return CodeGraph(idx)


def test_symbol_table():
    g = _mk()
    assert g.symbol_table["a"][0].path == "a.py"


def test_callers_of():
    g = _mk()
    callers = g.callers_of("b")          # a.py refs b
    assert any(s.name == "a" for s in callers)


def test_dependents_of():
    g = _mk()
    dep = g.dependents_of(["b"], depth=2)  # b is referenced by a
    assert "a.py" in dep
