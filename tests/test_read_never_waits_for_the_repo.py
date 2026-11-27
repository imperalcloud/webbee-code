"""A read never waits for the whole repository (2026-11-27).

Live: the stall file of a terminal whose workspace was a home folder -- `read_file` stood 300 s in
``Linker.links()``, the table of every resolved use of every file, built on the reading thread for one file's
header. The terminal runs one request at a time, so it answered nothing else. The table is built off the reader's
path; a reader takes what is ready."""
import pathlib
import threading
import time
from types import SimpleNamespace

from webbee.intel import query as Q


def _svc(monkeypatch, build_s, table):
    built = []

    def links():
        time.sleep(build_s)
        built.append(1)
        return table
    svc = SimpleNamespace(graph=object())
    monkeypatch.setattr(Q, "linker", lambda s: SimpleNamespace(links=links))
    return svc, built


def test_a_table_that_takes_long_does_not_hold_the_reader(monkeypatch):
    svc, built = _svc(monkeypatch, 0.6, [("b.py", "", "call", 1, "a.py", "f", "import")])
    t = time.monotonic()
    assert Q.links_ready(svc, wait=0.05) is None
    assert Q.links_ready(svc, wait=0.05) is None             # the same build, not a second one
    assert time.monotonic() - t < 0.4
    assert len(Q.links_ready(svc, wait=3.0)) == 1 and built == [1]
    assert Q.links_ready(svc, wait=0.0) is not None and built == [1]     # ready: taken at once, built once


def test_a_changed_graph_is_a_new_table_and_an_old_build_never_passes_for_it(monkeypatch):
    svc, built = _svc(monkeypatch, 0.3, ["old"])
    assert Q.links_ready(svc, wait=0.01) is None
    svc.graph = object()                                     # the index changed while the table was being built
    time.sleep(0.5)
    assert getattr(svc, "_links_graph", None) is None        # the finished build was of another graph: not kept
    assert Q.links_ready(svc, wait=3.0) == ["old"] and len(built) == 2


def test_a_build_that_fails_leaves_no_table_and_no_wait(monkeypatch):
    svc = SimpleNamespace(graph=object())

    def boom(s):
        raise RuntimeError("x")
    monkeypatch.setattr(Q, "linker", boom)
    assert Q.links_ready(svc, wait=2.0) is None and Q.links_ready(SimpleNamespace(graph=None)) is None


def test_the_read_header_asks_only_for_what_is_ready():
    src = (pathlib.Path(Q.__file__).parents[1] / "tools.py").read_text(encoding="utf-8")
    head = src[src.index("def _intel_file_context"):src.index("def _intel_file_context") + 2600]
    assert "links_ready(self.indexer)" in head and ".links()" not in head


def test_a_tool_that_asks_for_the_links_says_what_it_waits_for(monkeypatch):
    svc, _ = _svc(monkeypatch, 0.5, [])
    svc.index = SimpleNamespace(files={})
    monkeypatch.setattr(Q, "LINKS_TOOL_WAIT_S", 0.05)
    monkeypatch.setattr(Q.linked, "__defaults__", (0.05,))
    for out in (Q.graph_slice(svc, ["f"]), Q.impact_of_change(svc, ["f"])):
        assert out["ok"] is False and "still being resolved" in out["content"]
    src = pathlib.Path(Q.__file__).read_text(encoding="utf-8")
    calls = [l for l in src.splitlines() if "linker(svc)" in l and not l.lstrip().startswith(("#", "def "))]
    assert len(calls) == 1 and "lk = linker(svc)" in calls[0]     # only the build itself asks for the resolver
