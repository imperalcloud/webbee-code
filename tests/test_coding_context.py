from webbee.coding_context import WALK_IGNORE_DIRS, build_coding_context


def test_walk_ignore_dirs_has_the_heavy_hitters():
    for d in ("node_modules", "vendor", "dist", "build", "target", "__pycache__"):
        assert d in WALK_IGNORE_DIRS


def test_build_coding_context_prunes_heavy_dirs(tmp_path):
    # W6: the per-turn file-tree walk must not descend node_modules/etc. — on a
    # real repo it's slow and (with the 200-file cap) would fill the snapshot
    # with dependency junk instead of the user's own code.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x\n")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("y\n")
    tree = build_coding_context(str(tmp_path))["tree"]
    assert "src/a.py" in tree
    assert "node_modules" not in tree


def test_build_coding_context_still_prunes_dotdirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x\n")
    hidden = tmp_path / ".secret"
    hidden.mkdir()
    (hidden / "k.txt").write_text("y\n")
    tree = build_coding_context(str(tmp_path))["tree"]
    assert "src/a.py" in tree
    assert ".secret" not in tree
