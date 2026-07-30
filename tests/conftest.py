"""Shared test isolation, autouse across the whole suite.

Mode persistence (T6.1) writes a tiny per-repo marker to
`~/.cache/webbee/mode-{repo_key}` on every mode change -- and the fallback
REPL loop most tests drive runs with `cwd` inside THIS repo checkout, so an
un-isolated run would repeatedly overwrite the developer's REAL cache entry
for this very repo (and could clobber a real webbee session's remembered
mode with a `pytest` run). Redirecting `webbee.mode_store._CACHE_DIR` to a
per-test tmp dir keeps every test hermetic, same spirit as this file's own
`_NoopIntel`/`shadow_factory=lambda cfg, ws: None` test doubles that already
keep intel/shadow off the developer's real ~/.cache/webbee/intel."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_mode_cache(tmp_path, monkeypatch):
    import webbee.mode_store as mode_store
    monkeypatch.setattr(mode_store, "_CACHE_DIR", str(tmp_path / "webbee-mode-cache"))


@pytest.fixture(autouse=True)
def _isolate_tier_cache(tmp_path, monkeypatch):
    """webbee-code-model-tier-slash-command-v1: tier_store.py writes a tiny
    per-repo marker to ~/.cache/webbee/tier-{repo_key}, sibling of mode_store
    -- same isolation rationale as _isolate_mode_cache above."""
    import webbee.tier_store as tier_store
    monkeypatch.setattr(tier_store, "_CACHE_DIR", str(tmp_path / "webbee-tier-cache"))


@pytest.fixture(autouse=True)
def _isolate_instance_lock_cache(tmp_path, monkeypatch):
    """0.3.25 Part C: the per-repo instance lock writes a real flock'd file
    under `~/.cache/webbee/instance-{repo_key}.lock` -- same rationale as
    `_isolate_mode_cache` above (never touch the developer's REAL cache, and
    keep every test's lock file hermetic to ITS OWN tmp dir so two unrelated
    tests can never see each other's lock as "already held")."""
    import webbee.instance_lock as instance_lock
    monkeypatch.setattr(instance_lock, "_CACHE_DIR", str(tmp_path / "webbee-instance-lock-cache"))


@pytest.fixture(autouse=True)
def _isolate_newtab_mode_cache(tmp_path, monkeypatch):
    """W5: Home's Settings tile persists the new-tab default mode to
    `~/.cache/webbee/newtab-mode` -- redirect it to a per-test tmp dir, same
    rationale as `_isolate_mode_cache` above (never touch the developer's
    real cache; keep every test hermetic)."""
    import webbee.newtab_mode as newtab_mode
    monkeypatch.setattr(newtab_mode, "_CACHE_DIR", str(tmp_path / "webbee-newtab-cache"))


@pytest.fixture(autouse=True)
def _reset_mode_store_repo_key_cache():
    """2026-07-25: `mode_store._KEY_CACHE` memoises repo_key per workspace so
    Shift-TAB stops paying for a `git remote get-url` on every mode switch
    (~13ms -> ~0.2ms). It is process-global by design -- a workspace's git
    remote cannot change under a running dock -- but in a TEST process it
    would outlive the test that filled it, so a later test that patches
    `compute_repo_key` for the SAME workspace path would silently read the
    earlier test's key. Today every test uses a `tmp_path`-unique workspace,
    so nothing collides; clearing it around each test makes that hold BY
    CONSTRUCTION instead of by luck.
    """
    import webbee.mode_store as mode_store
    import webbee.tier_store as tier_store
    mode_store._KEY_CACHE.clear()
    tier_store._KEY_CACHE.clear()
    yield
    mode_store._KEY_CACHE.clear()
    tier_store._KEY_CACHE.clear()
