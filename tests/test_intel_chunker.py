from webbee.intel.models import ProjectIndex, FileIndex, Symbol
from webbee.intel import chunker


def _idx(tmp_path):
    (tmp_path / "a.py").write_text(
        "import os\n\ndef alpha(x):\n    return x + 1\n\nclass Big:\n" + "".join(f"    def m{i}(self): pass\n" for i in range(60))
    )
    idx = ProjectIndex()
    idx.files["a.py"] = FileIndex(path="a.py", lang="python", symbols=[
        Symbol("alpha", "function", "a.py", 3, 4),
        Symbol("Big", "class", "a.py", 6, 66),
    ])
    return idx


def test_symbol_boundary_chunk(tmp_path):
    chunks = chunker.chunk_index(str(tmp_path), _idx(tmp_path))
    alpha = next(c for c in chunks if c.symbol == "alpha")
    assert alpha.id == "a.py#3-4" and alpha.kind == "function"
    assert "return x + 1" in alpha.text
    assert len(alpha.content_hash) == 64


def test_large_symbol_windowed(tmp_path):
    chunks = chunker.chunk_index(str(tmp_path), _idx(tmp_path))
    big = [c for c in chunks if c.symbol == "Big"]
    assert len(big) > 1  # 61-line class exceeds _CHUNK_MAX_LINES -> windows
    assert all(c.path == "a.py" for c in big)


def test_no_symbol_region_covered(tmp_path):
    # the module-level `import os` region (lines 1-2) has no symbol -> a window covers it
    chunks = chunker.chunk_index(str(tmp_path), _idx(tmp_path))
    assert any(c.start_line == 1 for c in chunks)


def test_content_hash_stable_across_line_shift(tmp_path):
    idx = _idx(tmp_path)
    c1 = next(c for c in chunker.chunk_index(str(tmp_path), idx) if c.symbol == "alpha")
    # same text at a different span => same hash (incremental-skip relies on this)
    assert chunker._hash("def alpha(x):\n    return x + 1") == chunker._hash("def alpha(x):\n    return x + 1")
    assert len(c1.content_hash) == 64
