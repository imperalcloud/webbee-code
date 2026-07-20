"""Auto-worktree isolation (W4b T5): create_worktree/is_clean/list_worktrees
against REAL temp git repos — no mocking of subprocess, since the whole
point is that git itself does the isolating."""
import os
import subprocess

from webbee.worktrees import (create_worktree, is_clean, list_worktrees,
                              worktree_path)


def _mk_repo(tmp_path, name="proj"):
    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    (root / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return root


def test_worktree_path_is_pure_and_deterministic(tmp_path):
    root = tmp_path / "myrepo"
    p1 = worktree_path(str(root), "ab12cd")
    p2 = worktree_path(str(root), "ab12cd")
    assert p1 == p2
    assert p1.endswith("myrepo-ab12cd")


def test_create_worktree_succeeds_against_a_real_repo(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "cache" / "worktrees"))
    root = _mk_repo(tmp_path)

    path = create_worktree(str(root), "abc123")
    assert path is not None
    assert os.path.isdir(path)
    assert os.path.exists(os.path.join(path, "README.md"))    # HEAD checked out
    # It's a genuine linked worktree, not a copy -- git says so.
    listing = subprocess.run(["git", "-C", str(root), "worktree", "list"],
                             capture_output=True, text=True, check=True).stdout
    assert path in listing


def test_create_worktree_returns_none_for_a_non_git_directory(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "cache" / "worktrees"))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert create_worktree(str(plain), "abc123") is None


def test_create_worktree_returns_none_when_git_binary_missing(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "cache" / "worktrees"))
    root = _mk_repo(tmp_path)

    def boom(*a, **kw):
        raise OSError("git not found")
    monkeypatch.setattr(WT.subprocess, "run", boom)

    assert create_worktree(str(root), "abc123") is None


def test_create_worktree_returns_none_on_timeout(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "cache" / "worktrees"))
    root = _mk_repo(tmp_path)

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)
    monkeypatch.setattr(WT.subprocess, "run", timeout)

    assert create_worktree(str(root), "abc123") is None


def test_is_clean_true_for_a_freshly_created_worktree(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "cache" / "worktrees"))
    root = _mk_repo(tmp_path)
    path = create_worktree(str(root), "abc123")

    assert is_clean(path) is True


def test_is_clean_false_after_an_uncommitted_edit(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "cache" / "worktrees"))
    root = _mk_repo(tmp_path)
    path = create_worktree(str(root), "abc123")
    with open(os.path.join(path, "README.md"), "a") as f:
        f.write("dirty\n")

    assert is_clean(path) is False


def test_is_clean_false_for_a_non_git_directory(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert is_clean(str(plain)) is False


def test_list_worktrees_empty_when_cache_root_absent(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "never-created"))
    assert list_worktrees() == []


def test_list_worktrees_lists_every_created_worktree_dir(tmp_path, monkeypatch):
    import webbee.worktrees as WT
    monkeypatch.setattr(WT, "WORKTREE_ROOT", str(tmp_path / "cache" / "worktrees"))
    root = _mk_repo(tmp_path)
    p1 = create_worktree(str(root), "aaa111")
    p2 = create_worktree(str(root), "bbb222")

    listing = list_worktrees()
    assert sorted(listing) == sorted([p1, p2])
