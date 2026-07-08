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
