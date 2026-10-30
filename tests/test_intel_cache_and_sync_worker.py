"""The local code index stays true to the disk across restarts, says when it no longer knows it is, and its cloud
sync never piles threads up (review 2026-10-30, H7 + M8): the cache of the first build for a HEAD was loaded as is
(edits made while Webbee was closed stayed invisible), index_fresh stayed true with no watcher, and every watcher
batch started its own sync thread that waited on one lock behind a long first sync."""
import asyncio
import os
import subprocess
import threading
import time

import pytest

pytest.importorskip("tree_sitter")
from webbee.intel import service as svc_mod  # noqa: E402
from webbee.intel import store, watch  # noqa: E402
from webbee.intel.service import IntelService  # noqa: E402


def _git_repo(path):
    def git(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (path / "a.py").write_text("def alpha():\n    return 1\n")
    (path / "gone.py").write_text("def going():\n    return 1\n")
    git("add", "-A")
    git("commit", "-qm", "c0")
    return git


def _names(svc):
    return {s.name for fi in svc.index.files.values() for s in fi.symbols}


def _bump(p, text):
    """Write and move the mtime on: two writes within one clock tick must still differ."""
    p.write_text(text)
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


# ── H7: the cache is checked against the disk ──────────────────────────────

def test_edits_made_while_closed_are_reparsed_at_the_same_head(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo)
    cache = str(tmp_path / "cache")
    first = IntelService(str(repo), "rk", cache_dir=cache)
    first.build()
    assert {"alpha", "going"} <= _names(first)

    # Webbee closed: an edit, a deleted file, a new file -- the HEAD unchanged
    _bump(repo / "a.py", "def alpha_renamed():\n    return 2\n")
    (repo / "gone.py").unlink()
    (repo / "new.py").write_text("def fresh_one():\n    return 3\n")

    parsed = []
    real = svc_mod.indexer.build_index
    monkeypatch.setattr(svc_mod.indexer, "build_index", lambda root, files: parsed.append(sorted(files)) or real(root, files))
    second = IntelService(str(repo), "rk", cache_dir=cache)
    second.build()
    names = _names(second)
    assert "alpha_renamed" in names and "alpha" not in names
    assert "going" not in names and "gone.py" not in second.index.files
    assert "fresh_one" in names
    assert parsed == [["a.py", "new.py"]]                 # only what the stat check found changed is re-read
    if second.vectors is not None:                        # the embed extra: the cached vectors follow the disk too
        files = {i.split("#", 1)[0] for i in second.vectors.ids() if "#" in i}
        assert "gone.py" not in files and "new.py" in files

    # the reconciled index was saved: a third start re-reads nothing
    parsed.clear()
    third = IntelService(str(repo), "rk", cache_dir=cache)
    third.build()
    assert parsed == [] and "alpha_renamed" in _names(third)


def test_an_unchanged_tree_loads_the_cache_without_reading_a_file(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo)
    cache = str(tmp_path / "cache")
    IntelService(str(repo), "rk", cache_dir=cache).build()
    monkeypatch.setattr(svc_mod.indexer, "build_index", lambda *a: pytest.fail("a full index on an unchanged tree"))
    svc = IntelService(str(repo), "rk", cache_dir=cache)
    svc.build()
    assert {"alpha", "going"} <= _names(svc)


def test_a_cache_from_before_the_stats_reparses_once(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo)
    cache = str(tmp_path / "cache")
    svc = IntelService(str(repo), "rk", cache_dir=cache)
    svc.build()
    store.save(cache, "rk", svc.index)                    # the old shape: no stats
    _bump(repo / "a.py", "def beta():\n    return 2\n")
    again = IntelService(str(repo), "rk", cache_dir=cache)
    again.build()
    assert "beta" in _names(again) and "alpha" not in _names(again)
    assert store.load_with_stats(cache, "rk", again.git_ref)[1]           # saved with stats now


def test_changes_are_saved_to_the_cache_debounced(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo)
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(svc_mod, "SAVE_DEBOUNCE_S", 0.3)
    svc = IntelService(str(repo), "rk", cache_dir=cache)
    svc.build()
    saves = []
    real_save = store.save
    monkeypatch.setattr(store, "save", lambda *a, **k: saves.append(1) or real_save(*a, **k))
    for i in range(5):                                    # a burst of edits: one save, not five
        _bump(repo / "a.py", f"def edit_{i}():\n    return {i}\n")
        svc.apply_changes(["a.py"])
    assert saves == []                                    # never on every change
    deadline = time.time() + 5
    while not saves and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.4)
    assert saves == [1]
    idx, stats = store.load_with_stats(cache, "rk", svc.git_ref)
    assert "edit_4" in {s.name for s in idx.files["a.py"].symbols}
    assert stats["a.py"][1] == os.stat(repo / "a.py").st_size

    # the next start trusts what was saved: the session's edits are not re-read
    monkeypatch.setattr(svc_mod.indexer, "build_index", lambda *a: pytest.fail("re-read a saved edit"))
    restarted = IntelService(str(repo), "rk", cache_dir=cache)
    restarted.build()
    assert "edit_4" in _names(restarted)


def test_flush_cache_saves_a_pending_change_now(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_repo(repo)
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(svc_mod, "SAVE_DEBOUNCE_S", 60)
    svc = IntelService(str(repo), "rk", cache_dir=cache)
    svc.build()
    (repo / "b.py").write_text("def beta():\n    return 2\n")
    svc.apply_changes(["b.py"])
    svc.flush_cache()
    assert svc._save_timer is None
    assert "b.py" in store.load(cache, "rk", svc.git_ref).files


def test_apply_changes_follows_a_moved_head(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git = _git_repo(repo)
    svc = IntelService(str(repo), "rk", cache_dir=str(tmp_path / "cache"))
    svc.build()
    before = svc.git_ref
    (repo / "a.py").write_text("def alpha():\n    return 9\n")
    git("commit", "-qam", "c1")
    svc.apply_changes(["a.py"])
    svc.flush_cache()
    assert svc.git_ref != before and svc.index.git_ref == svc.git_ref
    assert svc.repo_profile()["git_ref"] == svc.git_ref[:12]
    assert store.load(str(tmp_path / "cache"), "rk", svc.git_ref) is not None      # saved under the new HEAD


# ── H7: fresh flips false when the watcher ends, and says why ──────────────

def _built(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.build()
    assert svc.repo_profile()["index_fresh"] is True and "index_stale_reason" not in svc.repo_profile()
    return svc


def test_a_crashed_watcher_makes_the_index_not_fresh(tmp_path):
    svc = _built(tmp_path)

    def broken(_root, **kw):
        async def gen():
            raise RuntimeError("inotify went away")
            yield
        return gen()
    asyncio.run(watch.watch_workspace(svc.root, svc.apply_changes, awatch=broken))
    prof = svc.repo_profile()
    assert prof["index_fresh"] is False
    assert "inotify went away" in prof["index_stale_reason"]


def test_no_watchfiles_makes_the_index_not_fresh(tmp_path, monkeypatch):
    import builtins
    svc = _built(tmp_path)
    real_import = builtins.__import__

    def blocked(name, *a, **kw):
        if name == "watchfiles":
            raise ImportError("no watchfiles")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", blocked)
    asyncio.run(watch.watch_workspace(svc.root, svc.apply_changes))
    prof = svc.repo_profile()
    assert prof["index_fresh"] is False and "watchfiles is not installed" in prof["index_stale_reason"]


def test_a_watcher_that_ends_reports_it_and_a_live_one_does_not(tmp_path):
    svc = _built(tmp_path)
    ends = []

    def once(_root, **kw):
        async def gen():
            yield {("modified", os.path.join(svc.root, "a.py"))}
            assert svc.fresh is True and ends == []          # running: still fresh, nothing reported
        return gen()
    asyncio.run(watch.watch_workspace(svc.root, svc.apply_changes, awatch=once, on_end=ends.append))
    assert ends == ["the file watcher ended"]


def test_a_rebuild_makes_the_index_fresh_again(tmp_path):
    svc = _built(tmp_path)
    svc.watcher_ended("test")
    assert svc.repo_profile()["index_fresh"] is False
    svc.build()
    assert svc.repo_profile()["index_fresh"] is True and svc.stale_reason == ""


# ── M8: one sync worker, merged requests, a bounded sync_now ──────────────

class _Slow:
    """Stands in for _sync_vault: the first run blocks until released; every run is recorded."""

    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(self, rels=None, history=True):
        self.calls.append((None if rels is None else sorted(rels), history))
        self.started.set()
        self.release.wait(10)
        return {"repo_key": "k"}


def _service_with_slow_sync(tmp_path, monkeypatch):
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.vault_client_factory = lambda: object()
    slow = _Slow()
    monkeypatch.setattr(svc, "_sync_vault", slow)
    return svc, slow


def _sync_threads():
    return [t for t in threading.enumerate() if t.name == "webbee-code-sync" and t.is_alive()]


def _wait_idle(svc, timeout=5):
    deadline = time.time() + timeout
    while svc._sync_busy() or svc._sync_worker is not None:
        assert time.time() < deadline, "the sync worker never went idle"
        time.sleep(0.01)


def test_watcher_batches_during_a_slow_sync_merge_into_one(tmp_path, monkeypatch):
    svc, slow = _service_with_slow_sync(tmp_path, monkeypatch)
    before = len(_sync_threads())
    svc.sync_vault(None)                                   # the long first sync
    assert slow.started.wait(5)
    for i in range(20):                                    # 20 watcher batches while it runs
        svc.sync_vault([f"f{i % 7}.py"])
    assert len(_sync_threads()) - before == 1              # one worker, never a thread per batch
    slow.release.set()
    _wait_idle(svc)
    assert slow.calls == [(None, True), ([f"f{i}.py" for i in range(7)], False)]   # one merged follow-up
    assert len(_sync_threads()) == before                  # the worker ends when nothing is pending


def test_a_full_request_supersedes_pending_paths(tmp_path, monkeypatch):
    svc, slow = _service_with_slow_sync(tmp_path, monkeypatch)
    svc.sync_vault(["x.py"])
    assert slow.started.wait(5)
    svc.sync_vault(["a.py"])
    svc.sync_vault(None, history=False)                    # a full listing lists a.py too
    svc.sync_vault(["b.py"])                               # beside a pending full one: adds nothing
    slow.release.set()
    _wait_idle(svc)
    assert slow.calls == [(["x.py"], False), (None, False)]


def test_sync_if_due_never_starts_beside_a_queued_or_running_sync(tmp_path, monkeypatch):
    svc, slow = _service_with_slow_sync(tmp_path, monkeypatch)
    svc.index = object()
    svc.sync_vault(["x.py"])
    assert slow.started.wait(5)
    assert svc.sync_if_due() is False                      # running
    slow.release.set()
    _wait_idle(svc)
    assert svc.sync_if_due() is True                       # idle and never synced: due
    _wait_idle(svc)
    assert slow.calls[-1] == (None, False)


def test_sync_now_waits_a_bounded_time_and_says_busy(tmp_path, monkeypatch):
    svc, slow = _service_with_slow_sync(tmp_path, monkeypatch)
    svc.last_sync = {"repo_key": "k"}                      # not the first sync
    svc.sync_vault(None)
    assert slow.started.wait(5)
    t0 = time.monotonic()
    out = svc.sync_now(wait_s=0.2)
    assert time.monotonic() - t0 < 2
    assert out["ok"] is False and out["content"].startswith("sync busy:")
    slow.release.set()
    _wait_idle(svc)
    assert slow.calls == [(None, True), (None, False)]     # its request stayed queued and went after


def test_sync_now_answers_with_its_own_run(tmp_path, monkeypatch):
    svc, slow = _service_with_slow_sync(tmp_path, monkeypatch)
    slow.release.set()
    out = svc.sync_now(wait_s=5)
    assert out["repo_key"] == "k" and out["synced_at"] > 0
    assert slow.calls == [(None, False)]


def test_the_first_sync_running_still_answers_at_once(tmp_path, monkeypatch):
    svc, slow = _service_with_slow_sync(tmp_path, monkeypatch)
    svc.sync_vault(None)
    assert slow.started.wait(5)
    out = svc.sync_now()
    assert out == {"ok": False, "content": "the first code sync of this repo is still running"}
    slow.release.set()
    _wait_idle(svc)


def test_a_sync_that_raises_never_ends_the_worker(tmp_path, monkeypatch):
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.vault_client_factory = lambda: object()
    calls = []

    def flaky(rels=None, history=True):
        calls.append(rels)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"repo_key": "k"}
    monkeypatch.setattr(svc, "_sync_vault", flaky)
    svc.sync_vault(["a.py"])
    _wait_idle(svc)
    assert "boom" in svc.last_sync_error
    assert svc.sync_now(wait_s=5)["repo_key"] == "k"
