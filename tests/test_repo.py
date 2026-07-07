"""U0 repo identity: stable repo_key from git remote (fallback: root path);
subdirectory launches map to the SAME key (spec §2.2)."""
import os
import subprocess

from webbee.repo import compute_repo_key, find_repo_root


def _mk_repo(tmp_path, remote=None):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", remote],
                       check=True)
    return root


def test_find_repo_root_walks_up(tmp_path):
    root = _mk_repo(tmp_path)
    assert find_repo_root(str(root / "src")) == os.path.realpath(str(root))


def test_repo_key_same_from_subdir(tmp_path):
    root = _mk_repo(tmp_path, remote="https://git.example.com/a/b.git")
    k_root = compute_repo_key(find_repo_root(str(root)))
    k_sub = compute_repo_key(find_repo_root(str(root / "src")))
    assert k_root == k_sub
    assert len(k_root) == 12 and all(c in "0123456789abcdef" for c in k_root)


def test_repo_key_falls_back_to_path_for_non_git(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    k = compute_repo_key(find_repo_root(str(d)))
    assert len(k) == 12
