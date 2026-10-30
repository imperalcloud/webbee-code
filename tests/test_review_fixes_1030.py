"""The 2026-10-30 review of the whole client: what the cloud code map is sent, what a failed edit says back, what a long
output keeps, where glob may look, which root a written file is re-indexed under."""
import os
import subprocess

import pytest

import webbee.tools as T


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


def test_a_gitignored_code_file_never_reaches_the_cloud(tmp_path):
    pytest.importorskip("tree_sitter")
    from webbee.intel import service as S
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("settings_local.py\ngen/\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "settings_local.py").write_text("KEY = 'secret'\n")
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "out.py").write_text("y = 2\n")
    files, partial = S._code_files(str(tmp_path))
    assert files == ["app.py"] and partial == ""


def test_a_capped_listing_is_sent_as_partial(tmp_path, monkeypatch):
    pytest.importorskip("tree_sitter")
    from webbee.intel import service as S
    for i in range(5):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(S, "LISTING_MAX", 3)
    files, partial = S._code_files(str(tmp_path))
    assert len(files) == 3 and "capped at 3" in partial
    listing, why = S._local_listing_of(str(tmp_path))
    assert len(listing) == 3 and why == partial


def test_an_edit_that_misses_shows_where_the_text_is_now(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    x = 1\n    return x\n")
    ex = T.LocalToolExecutor(str(tmp_path))
    ex.run("read_file", {"path": "a.py"})
    r = ex.run("edit_file", {"path": "a.py", "old": "def a():\n    x = 2\n    return x", "new": "pass"})
    assert r["ok"] is False and "old string not found" in r["content"]
    assert "line 2" in r["content"] and "    x = 1" in r["content"] and "     2|     x = 1" in r["content"]
    r = ex.run("multi_edit", {"edits": [{"path": "a.py", "old": "def a():\n\tx = 1", "new": "pass"}]})
    assert r["ok"] is False and "different whitespace" in r["content"]


def test_a_long_output_keeps_its_head_and_its_tail():
    text = "HEAD-LINE\n" + "x" * 100_000 + "\nSUMMARY: 3 failed, 97 passed"
    out = T._truncated(text, 30_000)
    assert out.startswith("HEAD-LINE") and out.endswith("SUMMARY: 3 failed, 97 passed") and "TRUNCATED" in out
    assert T._truncated("short", 30_000) == "short"


def test_glob_stays_inside_the_workspace_and_is_bounded(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("")
    ex = T.LocalToolExecutor(str(tmp_path))
    assert ex.run("glob", {"pattern": "/etc/*"})["ok"] is False
    assert ex.run("glob", {"pattern": "../*"})["ok"] is False
    assert ex.run("glob", {"pattern": "*.py"})["content"] == "a.py"
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("")
    monkeypatch.setattr(T, "_GLOB_MAX", 2)
    r = ex.run("glob", {"pattern": "*.txt"})
    assert r["content"].count("\n") == 2 and "3 more match(es)" in r["content"]


def test_a_written_file_is_reindexed_under_the_index_root(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    seen = []

    class Idx:
        root = str(tmp_path)
        ready = True

        def apply_changes(self, rels):
            seen.extend(rels)

        def sync_if_due(self):
            return False
    ex = T.LocalToolExecutor(str(sub), indexer=Idx())
    ex.run("write_file", {"path": "mod.py", "content": "x = 1\n"})
    assert seen == [os.path.join("pkg", "mod.py")]
