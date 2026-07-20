"""Mode persistence per-repo (T6.1): load_mode/save_mode roundtrip through
~/.cache/webbee/mode-{repo_key}, the autopilot-never-persisted security
posture, and fail-soft behavior on a corrupt/missing cache. The real cache
dir is never touched -- conftest.py's autouse `_isolate_mode_cache` fixture
redirects `webbee.mode_store._CACHE_DIR` to a per-test tmp path."""
import os

from webbee.mode_store import load_mode, save_mode


def _fake_repo(tmp_path, monkeypatch, key="abc123def456"):
    """A repo identity fixed to `key` regardless of the actual workspace
    path -- load_mode/save_mode both resolve THIS repo's key via
    webbee.repo.compute_repo_key(find_repo_root(workspace)), so a real git
    subprocess never needs to run for these tests."""
    import webbee.mode_store as MS
    monkeypatch.setattr(MS, "compute_repo_key", lambda root: key)
    monkeypatch.setattr(MS, "find_repo_root", lambda start: start)
    return str(tmp_path / "some-workspace")


def test_load_mode_returns_none_when_no_file_yet(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    assert load_mode(ws) is None


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    save_mode(ws, "plan")
    assert load_mode(ws) == "plan"


def test_save_writes_under_cache_dir_named_by_repo_key(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch, key="deadbeef0000")
    save_mode(ws, "plan")
    import webbee.mode_store as MS
    path = os.path.join(MS._CACHE_DIR, "mode-deadbeef0000")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        assert f.read().strip() == "plan"


def test_save_overwrites_previous_mode(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    save_mode(ws, "plan")
    save_mode(ws, "default")
    assert load_mode(ws) == "default"


def test_two_different_repos_never_share_a_slot(tmp_path, monkeypatch):
    import webbee.mode_store as MS
    ws_a = str(tmp_path / "a")
    ws_b = str(tmp_path / "b")
    keys = {ws_a: "repoAAAAAAAA", ws_b: "repoBBBBBBBB"}
    monkeypatch.setattr(MS, "find_repo_root", lambda start: start)
    monkeypatch.setattr(MS, "compute_repo_key", lambda root: keys[root])

    save_mode(ws_a, "plan")
    save_mode(ws_b, "default")
    assert load_mode(ws_a) == "plan"
    assert load_mode(ws_b) == "default"


# ── autopilot is NEVER persisted (security posture) ───────────────────────────

def test_save_autopilot_is_downgraded_to_default_on_disk(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    save_mode(ws, "autopilot")
    assert load_mode(ws) == "default"


def test_save_autopilot_never_written_verbatim_even_over_an_existing_plan(tmp_path, monkeypatch):
    # A previously-remembered non-autopilot mode must not survive as
    # "autopilot" no matter what was there before the upgrade.
    ws = _fake_repo(tmp_path, monkeypatch)
    save_mode(ws, "plan")
    save_mode(ws, "autopilot")
    assert load_mode(ws) == "default"


# ── fail-soft on a corrupt/unwritable cache ───────────────────────────────────

def test_load_corrupt_file_returns_none_not_raise(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch, key="corruptkey01")
    import webbee.mode_store as MS
    os.makedirs(MS._CACHE_DIR, exist_ok=True)
    path = os.path.join(MS._CACHE_DIR, "mode-corruptkey01")
    with open(path, "wb") as f:
        f.write(b"\xff\xfe\x00garbage-not-utf8-safe\xff")
    # Either decodes to something harmless or raises internally -- either
    # way load_mode must never propagate the error.
    assert load_mode(ws) is None or isinstance(load_mode(ws), str)


def test_load_blank_file_returns_none(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch, key="blankkey0001")
    import webbee.mode_store as MS
    os.makedirs(MS._CACHE_DIR, exist_ok=True)
    path = os.path.join(MS._CACHE_DIR, "mode-blankkey0001")
    with open(path, "w", encoding="utf-8") as f:
        f.write("   \n")
    assert load_mode(ws) is None


def test_load_survives_repo_key_computation_failure(monkeypatch):
    import webbee.mode_store as MS

    def boom(root):
        raise OSError("no git binary")
    monkeypatch.setattr(MS, "find_repo_root", lambda start: start)
    monkeypatch.setattr(MS, "compute_repo_key", boom)
    assert load_mode("/anywhere") is None


def test_save_survives_unwritable_cache_dir(monkeypatch):
    import webbee.mode_store as MS
    # A cache dir that can never be created -- /dev/null is a FILE, so any
    # path nested under it fails os.makedirs with NotADirectoryError,
    # portably (no root/Linux-specific path needed). save_mode must
    # swallow the failure, never raise.
    monkeypatch.setattr(MS, "_CACHE_DIR", os.path.join(os.devnull, "webbee"))
    monkeypatch.setattr(MS, "find_repo_root", lambda start: start)
    monkeypatch.setattr(MS, "compute_repo_key", lambda root: "x")
    save_mode("/anywhere", "plan")   # must not raise
