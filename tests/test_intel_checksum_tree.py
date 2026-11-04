"""Final review 2026-11-04 of the universal index: the cache keeps a file's kind and works without git; one listing
(gitignore-aware) for build and refresh; changes found by the checksum tree; a stuck file never stops a build."""
import os
import subprocess
import time

from webbee.intel import service as svc_mod
from webbee.intel import tree
from webbee.intel.service import IntelService


def _svc(folder, tmp_path, key="k"):
    return IntelService(str(folder), key, cache_dir=str(tmp_path / "cache"))


def _bump(path, text):
    path.write_text(text)
    t = time.time() + 5
    os.utime(path, (t, t))


def test_a_document_is_still_a_document_after_a_restart_and_a_folder_without_git_is_cached(tmp_path, monkeypatch):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "plan.md").write_text("# Plan\nship\n")
    (folder / "app.py").write_text("def pay():\n    return 1\n")
    first = _svc(folder, tmp_path)
    first.build()
    first.flush_cache()
    reads = []
    real = svc_mod.IntelService._build_files
    monkeypatch.setattr(svc_mod.IntelService, "_build_files", lambda self, rels: reads.append(list(rels)) or real(self, rels))
    second = _svc(folder, tmp_path)
    touched, changed = second.load_or_build()
    assert reads == [] and touched == {} and not changed          # from the cache: nothing read again
    assert second.index.files["plan.md"].kind == "document" and second.index.files["app.py"].kind == "code"


def test_the_cache_check_reads_only_what_changed_new_and_drops_what_is_gone(tmp_path):
    folder = tmp_path / "docs"
    (folder / "a" / "b").mkdir(parents=True)
    (folder / "a" / "b" / "deep.md").write_text("# Deep\n")
    (folder / "keep.md").write_text("# Keep\n")
    (folder / "old.md").write_text("# Old\n")
    s = _svc(folder, tmp_path)
    s.build()
    s.flush_cache()
    _bump(folder / "a" / "b" / "deep.md", "# Deeper\n")
    (folder / "old.md").unlink()
    (folder / "new.md").write_text("# New\n")
    s2 = _svc(folder, tmp_path)
    touched, changed = s2.load_or_build()
    assert set(touched) == {"a/b/deep.md", "new.md", "old.md"} and changed
    assert "old.md" not in s2.index.files and s2.index.files["new.md"].symbols[0].name == "New"


def test_refresh_sees_new_and_deleted_files_and_skips_an_unchanged_folder(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "one.md").write_text("# One\n")
    s = _svc(folder, tmp_path)
    s.build()
    root_before = s.checksum()
    assert s.refresh()["changed"] == 0
    (folder / "two.md").write_text("# Two\n")
    (folder / "one.md").unlink()
    out = s.refresh()
    assert out["changed"] == 1 and out["gone"] == 1
    assert set(s.index.files) == {"two.md"} and s.checksum() != root_before


def test_a_git_repository_unchanged_needs_no_walk_and_an_ignored_file_is_never_read(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("local.md\n")
    (repo / "local.md").write_text("# Local secret notes\n")
    (repo / "README.md").write_text("# Readme\n")
    s = _svc(repo, tmp_path)
    s.build()
    assert set(s.index.files) == {"README.md"}
    walks = []
    real = svc_mod._index_listing
    monkeypatch.setattr(svc_mod, "_index_listing", lambda *a, **k: walks.append(1) or real(*a, **k))
    assert s.refresh() == {"unchanged": True} and walks == []
    _bump(repo / "README.md", "# Readme 2\n")
    assert s.refresh()["changed"] == 1 and walks == [1]
    (repo / ".gitignore").write_text("local.md\nREADME.md\n")      # now ignored: out of the index, unread
    assert s.refresh()["unlisted"] == 1 and s.index.files == {}


def test_the_tree_skips_an_unchanged_subtree():
    old = {f"big/{i}.md": [1, 1] for i in range(500)} | {"small/x.md": [1, 1]}
    new = dict(old) | {"small/x.md": [2, 1]}
    changed, gone, compared = tree.changed_files(old, new)
    # compared: the root, big/ (equal hash: none of its 500 files looked at) and small/
    assert changed == ["small/x.md"] and gone == [] and compared == 3


def stuck_job(root, rels):
    """A pool job that never ends on a file named ``stuck``."""
    if any("stuck" in r for r in rels):
        time.sleep(60)
    return svc_mod._build_chunk(root, rels)


def test_a_stuck_file_is_skipped_and_the_rest_is_indexed(tmp_path, monkeypatch):
    folder = tmp_path / "docs"
    folder.mkdir()
    for n in ("a", "b", "stuck", "c"):
        (folder / f"{n}.md").write_text(f"# {n}\n")
    monkeypatch.setattr(svc_mod, "CHUNK_TIMEOUT_S", 3.0)
    monkeypatch.setattr(svc_mod, "FILE_TIMEOUT_S", 3.0)
    s = _svc(folder, tmp_path)
    s._pool_job = stuck_job
    t0 = time.monotonic()
    s.load_or_build()
    assert set(s.index.files) == {"a.md", "b.md", "c.md"}
    assert s.progress()["skipped"]["timeout"] == 1 and time.monotonic() - t0 < 30
