"""The cloud code map kept fresh while a session works (2026-10-30: a user's repo map was 5 days old and a folder's
map too -- the client synced at start, on the watcher's changes and when the kernel asked before a code question;
a shell edit, a git pull or a failed start sync left the map old, and the failure was only in a local log)."""
import pytest

pytest.importorskip("tree_sitter")
from webbee.intel import service as svc_mod  # noqa: E402
from webbee.intel.service import IntelService  # noqa: E402


class _Cloud:
    """The gateway's /v1/memory/code/sync/* as a fake: records what the client sends."""

    def __init__(self):
        self.calls = []

    async def _request(self, method, path, json=None, timeout=60):
        self.calls.append((path, json))
        if path.endswith("/sync/plan"):
            return {"repo_key": json["meta"]["local_key"], "removed": [], "have_blobs": [],
                    "changed": [i["path"] for i in json["listing"]]}
        return {"files": len(json["files"]), "links": 0}


def _synced(svc, monkeypatch):
    import os
    os.makedirs(os.path.join(svc.root, ".git"), exist_ok=True)     # a repository: only one reaches the vault
    monkeypatch.setattr(type(svc), "sync_vault",
                        lambda self, rels=None, history=True: self._sync_vault(rels, history))


def _idle(svc):
    import time
    for _ in range(500):
        if not svc._sync_busy():
            return
        time.sleep(0.01)


def test_a_working_session_resyncs_the_map_when_it_is_due(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    cloud = _Cloud()
    svc.build()
    svc.attach_vault(lambda: cloud)
    assert svc.last_sync_ok_at > 0 and svc.sync_state()["synced_s_ago"] is not None
    svc._sync_vault(None, history=False)                    # the listing the vault confirms, remembered
    cloud.calls.clear()
    assert svc.sync_if_due() is True
    _idle(svc)
    assert cloud.calls == []                                # checked: nothing changed, no cloud call
    (tmp_path / "a.py").write_text("def alpha():\n    return 2\n")             # a shell edit no watcher saw
    assert svc.sync_if_due() is True                        # no clock: the change itself sends it
    _idle(svc)
    plan = cloud.calls[0][1]
    assert plan["meta"]["listing_complete"] is True
    assert [f["path"] for f in cloud.calls[1][1]["files"]] == ["a.py"]


def test_a_failed_sync_says_why_and_is_tried_again(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    svc.build()

    class Down:
        async def _request(self, *a, **kw):
            raise OSError("gateway down")
    svc.attach_vault(lambda: Down())
    assert svc.last_sync_ok_at == 0 and "gateway down" in svc.sync_state()["sync_error"]
    assert "gateway down" in svc.sync_now()["content"]
    assert svc.sync_if_due() is True                                          # never synced: due at once
    assert svc.repo_profile()["code_map"]["synced_s_ago"] is None


def test_no_second_sync_beside_a_running_one(tmp_path, monkeypatch):
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.index = object()
    svc.vault_client_factory = lambda: None
    started = []
    monkeypatch.setattr(type(svc), "sync_vault", lambda self, rels=None, history=True: started.append(history))
    with svc._sync_lock:
        assert svc.sync_if_due() is False
    assert svc.sync_if_due() is True and started == [False]                   # the history is not re-read


def test_every_tool_call_keeps_the_map_fresh(tmp_path):
    from webbee.tools import LocalToolExecutor
    calls = []

    class Idx:
        def sync_if_due(self):
            calls.append(1)
    ex = LocalToolExecutor(str(tmp_path), indexer=Idx())
    (tmp_path / "f.txt").write_text("x\n")
    ex.run("read_file", {"path": "f.txt"})
    ex.run("read_file", {"path": "f.txt"})
    assert calls == [1, 1]


def test_an_unchanged_repository_asks_the_cloud_nothing(tmp_path, monkeypatch):
    """The owner's folder of 7 repositories: every code question synced all 7 (2-4 s each, nothing changed) -- past
    sync_now's wait, so the terminal showed intel_sync failed. An unchanged full listing makes no cloud call."""
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    cloud = _Cloud()
    svc.build()
    svc.attach_vault(lambda: cloud)
    svc._sync_vault(None, history=False)                       # a full sync the vault took
    cloud.calls.clear()
    out = svc._sync_vault(None, history=False)
    assert cloud.calls == [] and out.get("skipped") == "unchanged" and out["repo_key"]
    (tmp_path / "a.py").write_text("def alpha():\n    return 22\n")
    svc._sync_vault(None, history=False)
    assert cloud.calls and [f["path"] for f in cloud.calls[1][1]["files"]] == ["a.py"]
    svc._sync_vault(["a.py"], history=False)                  # changed files went: the confirmed listing is
    cloud.calls.clear()                                       # forgotten, the next full sync asks the cloud
    svc._sync_vault(None, history=False)
    assert cloud.calls


def test_the_kernel_hears_pending_not_failed_while_the_first_sync_runs(tmp_path):
    from webbee.tools import LocalToolExecutor

    class Idx:
        def sync_if_due(self):
            return False

        def sync_now(self):
            return {"pending": "the first code sync of this workspace is still running"}
    r = LocalToolExecutor(str(tmp_path), indexer=Idx()).run("intel_sync_local", {})
    assert r["ok"] is True and r["content"]["pending"] and r["content"]["repo_key"] == ""
