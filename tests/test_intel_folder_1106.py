"""Report 06.11 #2: a folder whose repositories sit one level deeper (core/<repo>, modules/<repo>) with loose
scripts at its top was one 670-file project; the first sync answered "pending" for a whole session; the read
header and graph_slice linked files by a shared name."""
import os
import subprocess
import threading
import time

import pytest

from webbee.intel import service as sv
from webbee.intel.service import IntelService, find_repositories, is_repository, repo_of, sub_repositories


def _repo(path):
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(sv, "_REPOS_SEEN", {})
    monkeypatch.setattr(sv, "_KEYS_FILE", str(tmp_path / "keys" / "vault-keys.json"))


def test_repositories_one_level_deeper_are_found_and_make_a_folder(tmp_path):
    for r in ("core/wpanel-sdk", "modules/wpanel-tickets", "gateway"):
        _repo(tmp_path / r)
    _repo(tmp_path / "core/wpanel-sdk/vendor/inner")          # inside a repository: never its own unit
    (tmp_path / "requirements.txt").write_text("x\n")         # a manifest and loose scripts at the top
    (tmp_path / "check-paths-agree.py").write_text("print(1)\n")
    assert find_repositories(str(tmp_path)) == ["gateway", "core/wpanel-sdk", "modules/wpanel-tickets"]
    assert not is_repository(str(tmp_path))                    # a folder of repositories, not one project
    assert sorted(sub_repositories(str(tmp_path))) == ["core/wpanel-sdk", "gateway", "modules/wpanel-tickets"]
    assert repo_of("core/wpanel-sdk/src/a.py", sub_repositories(str(tmp_path))) == "core/wpanel-sdk"
    assert repo_of("check-paths-agree.py", sub_repositories(str(tmp_path))) == ""


def test_a_manifest_without_repositories_inside_is_a_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert is_repository(str(tmp_path))
    _repo(tmp_path / "solo")
    assert is_repository(str(tmp_path / "solo"))


class _Slow:
    def __init__(self, out=None):
        self.started, self.release, self.calls, self.out = threading.Event(), threading.Event(), [], out

    def __call__(self, rels=None, history=True):
        self.calls.append((rels, history))
        self.started.set()
        self.release.wait(10)
        return self.out if self.out is not None else {}


def _svc(tmp_path, monkeypatch, out=None):
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.vault_client_factory = lambda: object()
    slow = _Slow(out)
    monkeypatch.setattr(svc, "_sync_vault", slow)
    return svc, slow


def test_the_first_sync_running_answers_with_the_keys_confirmed_last_time(tmp_path, monkeypatch):
    sv.remember_keys(str(tmp_path), {"repo_key": "", "repos": {"gateway": "k1", "core/wpanel-sdk": "k2"}})
    svc, slow = _svc(tmp_path, monkeypatch)
    svc.sync_vault(None)
    assert slow.started.wait(5)
    got = svc.sync_now()
    assert "pending" in got and got["known"]["repos"] == {"gateway": "k1", "core/wpanel-sdk": "k2"}
    assert "cloud graph answers" in got["pending"]
    slow.release.set()


def test_pending_ends_with_the_first_run_even_when_it_failed(tmp_path, monkeypatch):
    """Every tool call queued a sync; the kernel's ask always met one running and 'pending' never ended."""
    svc, slow = _svc(tmp_path, monkeypatch, out={"repo_key": "", "partial_reason": "no repository here"})
    slow.release.set()
    svc.sync_vault(None)
    deadline = time.time() + 5
    while svc._sync_done == 0:
        assert time.time() < deadline
        time.sleep(0.01)
    slow.release.clear()
    svc.sync_vault(None)                                       # the next tool call's sync is running
    assert slow.started.wait(5)
    got = {}
    th = threading.Thread(target=lambda: got.update(svc.sync_now()))
    th.start()
    time.sleep(0.2)
    assert th.is_alive() and not got                           # waits for a real outcome, never "pending" again
    slow.release.set()
    th.join(10)
    assert got.get("ok") is False and "no repository here" in got["content"]


def test_graph_slice_callers_are_resolved_users(tmp_path):
    pytest.importorskip("tree_sitter_python")
    (tmp_path / "lib.py").write_text("def session_for_sid(sid):\n    return sid\n")
    (tmp_path / "api.py").write_text("from lib import session_for_sid\n\n\ndef me(sid):\n"
                                     "    return session_for_sid(sid)\n")
    (tmp_path / "other.py").write_text("def unrelated():\n    return 'session_for_sid'\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=False)
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.build()
    from webbee.intel.query import graph_slice
    item = graph_slice(svc, ["session_for_sid"])["data"]["items"][0]
    assert [(c["path"], c["user"], c["line"]) for c in item["callers"]] == [("api.py", "me", 5)]
