"""Per-repo single-instance advisory lock (0.3.25 Part C). The real cache dir
is never touched -- conftest.py's autouse `_isolate_instance_lock_cache`
fixture redirects `webbee.instance_lock._CACHE_DIR` to a per-test tmp path,
same discipline as mode_store's own isolation fixture."""
import os

from webbee.instance_lock import acquire


def test_first_acquire_on_a_fresh_repo_is_primary():
    lock = acquire("repoAAAAAAA1")
    assert lock.held is False
    lock.close()


def test_second_acquire_on_the_same_key_while_first_is_open_is_secondary():
    first = acquire("repoBBBBBBB2")
    assert first.held is False
    second = acquire("repoBBBBBBB2")
    assert second.held is True
    first.close()
    second.close()   # no-op -- a held=True result never owns an fd


def test_two_different_repo_keys_never_collide():
    a = acquire("repoCCCCCCC3")
    b = acquire("repoDDDDDDD4")
    assert a.held is False
    assert b.held is False
    a.close()
    b.close()


def test_lock_is_released_on_close_and_re_acquirable():
    first = acquire("repoEEEEEEE5")
    assert first.held is False
    first.close()
    second = acquire("repoEEEEEEE5")
    assert second.held is False   # the lock let go -- re-acquirable
    second.close()


def test_close_is_idempotent_and_never_raises():
    lock = acquire("repoFFFFFFF6")
    lock.close()
    lock.close()   # must not raise the second time


def test_close_on_a_secondary_result_never_raises():
    first = acquire("repoGGGGGGG7")
    second = acquire("repoGGGGGGG7")
    second.close()   # held=True, fd=None -- must be a harmless no-op
    first.close()


def test_lock_file_is_created_under_the_cache_dir_named_by_repo_key():
    acquire("repoHHHHHHH8").close()
    import webbee.instance_lock as IL
    path = os.path.join(IL._CACHE_DIR, "instance-repoHHHHHHH8.lock")
    assert os.path.isfile(path)


def test_never_raises_when_cache_dir_is_unwritable(monkeypatch):
    import webbee.instance_lock as IL
    # /dev/null is a FILE, so any path nested under it fails os.makedirs
    # with NotADirectoryError, portably (no root/Linux-specific path
    # needed) -- same trick test_mode_store.py uses for the same reason.
    monkeypatch.setattr(IL, "_CACHE_DIR", os.path.join(os.devnull, "webbee"))
    lock = IL.acquire("anything")   # must not raise
    assert lock.held is False
    lock.close()                    # must not raise either


def test_never_raises_when_fcntl_import_fails(monkeypatch):
    import builtins

    import webbee.instance_lock as IL

    real_import = builtins.__import__

    def _boom_import(name, *a, **kw):
        if name == "fcntl":
            raise ImportError("no fcntl on this platform")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom_import)
    lock = IL.acquire("repoNoFcntl1")
    assert lock.held is False
    lock.close()
