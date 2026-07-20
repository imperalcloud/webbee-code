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
def _isolate_instance_lock_cache(tmp_path, monkeypatch):
    """0.3.25 Part C: the per-repo instance lock writes a real flock'd file
    under `~/.cache/webbee/instance-{repo_key}.lock` -- same rationale as
    `_isolate_mode_cache` above (never touch the developer's REAL cache, and
    keep every test's lock file hermetic to ITS OWN tmp dir so two unrelated
    tests can never see each other's lock as "already held")."""
    import webbee.instance_lock as instance_lock
    monkeypatch.setattr(instance_lock, "_CACHE_DIR", str(tmp_path / "webbee-instance-lock-cache"))
