"""Model-tier persistence per-repo (webbee-code-model-tier-slash-command-v1):
load_tier/save_tier roundtrip through ~/.cache/webbee/tier-{repo_key}, and
fail-soft behavior on a corrupt/missing/unknown-tier cache. Mirrors
test_mode_store.py exactly -- same fixture shape, same rationale. The real
cache dir is never touched -- conftest.py's autouse `_isolate_tier_cache`
fixture redirects `webbee.tier_store._CACHE_DIR` to a per-test tmp path.
"""
import os

from webbee.tier_store import load_tier, save_tier, TIERS


def _fake_repo(tmp_path, monkeypatch, key="abc123def456"):
    import webbee.tier_store as TS
    monkeypatch.setattr(TS, "compute_repo_key", lambda root: key)
    monkeypatch.setattr(TS, "find_repo_root", lambda start: start)
    return str(tmp_path / "some-workspace")


def test_load_tier_returns_none_when_no_file_yet(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    assert load_tier(ws) is None


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    save_tier(ws, "supersmart")
    assert load_tier(ws) == "supersmart"


def test_save_writes_under_cache_dir_named_by_repo_key(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch, key="deadbeef0000")
    save_tier(ws, "ultrasmart")
    import webbee.tier_store as TS
    path = os.path.join(TS._CACHE_DIR, "tier-deadbeef0000")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        assert f.read().strip() == "ultrasmart"


def test_load_ignores_corrupt_or_unknown_tier(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    import webbee.tier_store as TS
    os.makedirs(TS._CACHE_DIR, exist_ok=True)
    path = os.path.join(TS._CACHE_DIR, "tier-abc123def456")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not-a-real-tier")
    assert load_tier(ws) is None


def test_save_empty_string_clears_the_choice(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    save_tier(ws, "smart")
    assert load_tier(ws) == "smart"
    save_tier(ws, "")
    assert load_tier(ws) is None


def test_save_rejects_unknown_tier_silently(tmp_path, monkeypatch):
    ws = _fake_repo(tmp_path, monkeypatch)
    save_tier(ws, "turbo-deluxe")
    assert load_tier(ws) is None


def test_tiers_tuple_matches_kernel_contract():
    assert TIERS == ("smart", "supersmart", "ultrasmart")
