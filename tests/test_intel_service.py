import pytest
pytest.importorskip("tree_sitter")
from webbee.intel.service import IntelService


def test_build_and_profile(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.ts").write_text("export function beta(){}\n")
    svc = IntelService(str(tmp_path), "rk1", cache_dir=str(tmp_path / "c"))
    svc.build()
    assert svc.ready and svc.graph is not None
    prof = svc.repo_profile()
    assert prof["file_count"] >= 2
    assert "python" in prof["languages"] and "typescript" in prof["languages"]
    # bounded: entry-point/sample lists are capped
    assert isinstance(prof.get("top_symbols"), list) and len(prof["top_symbols"]) <= 20


def test_not_ready_before_build(tmp_path):
    svc = IntelService(str(tmp_path), "rk2", cache_dir=str(tmp_path / "c"))
    assert svc.ready is False
    assert svc.graph is None and svc.index is None


def test_apply_changes_incrementally_reparses_changed_file(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk3", cache_dir=str(tmp_path / "c"))
    svc.build()
    names_before = {s.name for fi in svc.index.files.values() for s in fi.symbols}
    assert "alpha" in names_before

    (tmp_path / "a.py").write_text("def alpha():\n    return 1\ndef beta():\n    return 2\n")
    svc.apply_changes({"a.py"})
    names_after = {s.name for fi in svc.index.files.values() for s in fi.symbols}
    assert "beta" in names_after


def test_apply_changes_drops_deleted_file(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk4", cache_dir=str(tmp_path / "c"))
    svc.build()
    (tmp_path / "a.py").unlink()
    svc.apply_changes({"a.py"})
    assert "a.py" not in svc.index.files


def test_callers_of_excludes_the_defining_file_itself(tmp_path):
    # F5: a def's own name must not be recorded as a ref in its own file, or
    # callers_of(name)/impact_of_change(name) wrongly include the defining
    # file as its own caller/dependent.
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk5", cache_dir=str(tmp_path / "c"))
    svc.build()
    callers = svc.graph.callers_of("alpha")
    assert not any(s.path == "a.py" for s in callers)


def test_repo_profile_survives_concurrent_apply_changes(tmp_path, monkeypatch):
    # F3+F6: apply_changes now runs off the event loop (in a worker thread,
    # via watch.py's asyncio.to_thread), so a worker thread can be mutating
    # self.index.files while repo_profile() (called concurrently) iterates
    # it. Real-thread timing is too unreliable to force the interleave
    # deterministically, so this splices the mutation into the MIDDLE of
    # repo_profile's own Counter() consumption -- the same shape of race,
    # forced instead of hoped-for. Pre-fix, repo_profile iterates
    # idx.files.values() (the live dict) directly, so a same-iteration size
    # change raises "dictionary changed size during iteration". Post-fix, it
    # must snapshot `list(idx.files.values())` under the lock first and
    # compute everything from that snapshot, which a later live mutation
    # can't invalidate.
    for i in range(5):
        (tmp_path / f"seed_{i}.py").write_text(f"def seed_{i}():\n    return {i}\n")
    svc = IntelService(str(tmp_path), "rk_race", cache_dir=str(tmp_path / "c"))
    svc.build()

    import webbee.intel.service as service_mod
    real_counter = service_mod.Counter
    mutated = {"done": False}

    def spliced_counter(iterable):
        def _tap():
            for i, item in enumerate(iterable):
                if i == 1 and not mutated["done"]:
                    mutated["done"] = True
                    (tmp_path / "new_during_iter.py").write_text("def z():\n    return 1\n")
                    svc.apply_changes({"new_during_iter.py"})
                yield item
        return real_counter(_tap())

    monkeypatch.setattr(service_mod, "Counter", spliced_counter)
    svc.repo_profile()  # must not raise RuntimeError: dictionary changed size during iteration


def test_build_populates_vectors_and_profile(tmp_path):
    pytest.importorskip("model2vec")
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk_embed1", cache_dir=str(tmp_path / "c"))
    svc.build()
    assert svc.vectors is not None and svc.vectors_ready
    assert len(svc.vectors.ids()) >= 1
    prof = svc.repo_profile()
    assert prof["vectors_ready"] is True and prof["embedded_chunks"] >= 1


def test_apply_changes_incremental_embed(tmp_path):
    pytest.importorskip("model2vec")
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk_embed2", cache_dir=str(tmp_path / "c"))
    svc.build()
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    svc.apply_changes(["b.py"])
    ids = svc.vectors.ids()
    assert any(i.startswith("b.py#") for i in ids)


def test_apply_changes_removes_orphan_vector_on_span_shifting_edit(tmp_path):
    # F1: editing a file shifts later symbols' line-spans -> new chunk ids.
    # The OLD id (whose span no longer exists) must be removed, not left
    # behind to pollute kNN search forever.
    pytest.importorskip("model2vec")
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk_embed3", cache_dir=str(tmp_path / "c"))
    svc.build()
    old_ids = set(svc.vectors.ids())
    assert old_ids  # sanity: something was embedded

    # Prepend a line -- shifts alpha's span down by one line, changing its
    # chunk id even though its content (and hash) is unrelated to the diff.
    (tmp_path / "a.py").write_text("import os\ndef alpha():\n    return 1\n")
    svc.apply_changes({"a.py"})

    new_ids = set(svc.vectors.ids())
    assert not (old_ids & new_ids), f"orphan ids survived: {old_ids & new_ids}"
    assert any(i.startswith("a.py#") for i in new_ids)  # fresh span id present


def test_apply_changes_does_not_drop_unrelated_unchanged_chunks(tmp_path):
    # Guards against the naive "remove all `{rel}#` then add fresh" fix:
    # since fresh adds are hash-skipped, editing a LATE symbol must not
    # drop an EARLIER unchanged chunk that's still current.
    pytest.importorskip("model2vec")
    src = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    (tmp_path / "a.py").write_text(src)
    svc = IntelService(str(tmp_path), "rk_embed4", cache_dir=str(tmp_path / "c"))
    svc.build()
    ids_before = set(svc.vectors.ids())
    alpha_id = next(i for i in ids_before if i.startswith("a.py#"))

    # Append a new symbol after beta -- alpha's span/id is untouched, so its
    # chunk should survive (and not merely by re-adding it: it must never
    # have been removed in the first place).
    (tmp_path / "a.py").write_text(src + "\n\ndef gamma():\n    return 3\n")
    svc.apply_changes({"a.py"})

    ids_after = set(svc.vectors.ids())
    assert alpha_id in ids_after


def test_profile_carries_content_addressed_contract_summary(tmp_path):
    (tmp_path / "api.py").write_text(
        'from pydantic import BaseModel\n'
        'class Item(BaseModel):\n    name: str\n'
        '@app.post("/v1/items", response_model=Item)\n'
        'def create(body: Item): pass\n'
    )
    svc = IntelService(str(tmp_path), "rk_contract", cache_dir=str(tmp_path / "c"))
    svc.build()
    prof = svc.repo_profile()
    assert len(prof["content_digest"]) == 64
    assert prof["evidence_version"] == 1
    assert prof["endpoint_count"] == 1
    assert prof["schema_count"] == 1
    assert prof["contract_evidence_limit"] == 5000
    assert prof["contract_evidence_complete"] is True
    assert prof["endpoints"][0]["route"] == "/v1/items"
    assert prof["schemas"][0]["name"] == "Item"


# ── code graph everywhere (plan 3 Tasks 7, 9): the local workspace in the vault's graph ──

class _Cloud:
    """The gateway's /v1/memory/code/sync/* as a fake: records what the client sends."""

    def __init__(self, have=()):
        self.calls = []
        self.have = set(have)

    async def _request(self, method, path, json=None, timeout=60):
        self.calls.append((path, json))
        if path.endswith("/sync/plan"):
            return {"repo_key": json["meta"]["local_key"], "removed": [], "have_blobs": [],
                    "changed": [i["path"] for i in json["listing"] if i["path"] not in self.have]}
        return {"files": len(json["files"]), "links": 0}


def _synced(svc, monkeypatch):
    monkeypatch.setattr(type(svc), "sync_vault", lambda self, rels=None: self._sync_vault(rels))


def test_a_build_syncs_every_indexable_file_and_a_change_only_itself(tmp_path, monkeypatch):
    from webbee.repo import compute_repo_key
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.ts").write_text("export function beta(){}\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "setup.py").write_text("x\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    cloud = _Cloud()
    svc.build()
    svc.attach_vault(lambda: cloud)
    plan = cloud.calls[0][1]
    assert sorted(i["path"] for i in plan["listing"]) == ["a.py", "b.ts"]          # never .env / setup.py
    assert plan["meta"]["local_key"] == compute_repo_key(str(tmp_path)) and plan["meta"]["source"] == "local"
    assert plan["meta"]["machine"]["machine_id"] and plan["meta"]["listing_complete"] is True
    applied = cloud.calls[1][1]
    assert sorted(f["path"] for f in applied["files"]) == ["a.py", "b.ts"] and applied["blobs"][0]["facts"]["defs"]
    cloud.calls.clear()
    (tmp_path / "a.py").write_text("def alpha():\n    return 2\n")
    svc.apply_changes(["a.py"])
    plan = cloud.calls[0][1]
    assert [i["path"] for i in plan["listing"]] == ["a.py"] and plan["meta"]["listing_complete"] is False
    assert [f["path"] for f in cloud.calls[1][1]["files"]] == ["a.py"]


def test_no_cloud_no_sync_and_a_cloud_outage_never_touches_the_local_index(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    svc.build()
    assert svc._sync_vault() == {}

    class Down:
        async def _request(self, *a, **kw):
            raise OSError("gateway down")
    svc.attach_vault(lambda: Down())
    assert svc.ready and svc.last_sync == {}


def test_git_history_gives_changed_together_pairs_weighted_by_how_often(tmp_path, monkeypatch):
    import subprocess
    from webbee.intel import service as svc_mod

    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t"), git("config", "user.name", "t")
    for i, files in enumerate((("a.py", "b.py"), ("a.py", "b.py", "c.py"))):
        for f in files:
            (tmp_path / f).write_text(f"x = {i}\n")
        git("add", "-A")
        git("commit", "-qm", f"c{i}")
    pairs = {(e["src_path"], e["dst_path"]): e["confidence"] for e in svc_mod.co_change_pairs(str(tmp_path))}
    assert pairs[("a.py", "b.py")] > pairs[("a.py", "c.py")] and pairs[("b.py", "a.py")] == pairs[("a.py", "b.py")]
    assert all(e["origin"] == "changed together" for e in svc_mod.co_change_pairs(str(tmp_path)))

    def no_git(*a, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(svc_mod.subprocess, "run", no_git)
    assert svc_mod.co_change_pairs(str(tmp_path)) == []


def test_the_build_sync_carries_the_history_links(tmp_path, monkeypatch):
    from webbee.intel import service as svc_mod
    (tmp_path / "a.py").write_text("x = 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    monkeypatch.setattr(svc_mod, "co_change_pairs", lambda root: [{"src_path": "a.py", "dst_path": "b.py",
                                                                   "confidence": 0.4, "origin": "changed together"}])
    cloud = _Cloud()
    svc.build()
    svc.attach_vault(lambda: cloud)
    assert cloud.calls[-1][1]["measured"][0]["dst_path"] == "b.py"


def test_a_link_leading_out_of_the_workspace_is_never_read(tmp_path, monkeypatch):
    """Security review of 0.5.0: the local sync reads no symlink and nothing that resolves outside the root."""
    import os
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("SECRET = 'outside the workspace'\n")
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    os.symlink(outside, tmp_path / "leak.py")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    cloud = _Cloud()
    svc.build()
    svc.attach_vault(lambda: cloud)
    sent = [c[1] for c in cloud.calls if "files" in c[1]]
    assert sent and all(f["path"] != "leak.py" for c in sent for f in c["files"])
    assert "outside the workspace" not in repr(cloud.calls)


def test_sync_now_sends_an_edit_the_watcher_has_not_reported(tmp_path, monkeypatch):
    """frame phase A: the kernel asks for a sync before a code question -- the edit reaches the vault at once,
    even one no watcher reported (a shell edit), and without re-reading the git history."""
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    _synced(svc, monkeypatch)
    cloud = _Cloud()
    svc.build()
    svc.attach_vault(lambda: cloud)
    cloud.calls.clear()
    cloud.have = {"a.py"}
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")          # a shell edit, never reported
    out = svc.sync_now()
    assert out["repo_key"] and out["synced_at"] > 0
    assert [f["path"] for f in cloud.calls[1][1]["files"]] == ["b.py"]
    assert "measured" not in cloud.calls[1][1]


def test_sync_now_without_cloud_says_so(tmp_path):
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    assert svc.sync_now() == {"ok": False, "content": "this session cannot reach the cloud"}


def test_sync_now_while_the_first_sync_runs_answers_at_once(tmp_path, monkeypatch):
    """frame phase A (review I7): a big repo's first sync is still running -- the question goes coarse at once
    instead of waiting behind it."""
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.build()
    svc.vault_client_factory = lambda: _Cloud()
    svc.last_sync = {}
    assert svc._sync_lock.acquire(blocking=False)          # the boot-time first sync holds the lock
    try:
        out = svc.sync_now()
    finally:
        svc._sync_lock.release()
    assert out == {"ok": False, "content": "the first code sync of this repo is still running"}
