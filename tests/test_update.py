import json
from webbee.update import check_for_update


def test_notice_when_newer(tmp_path):
    msg = check_for_update("0.1.0", cache_path=tmp_path / "u.json", now=1000.0,
                           fetch=lambda: "0.2.0")
    assert msg and "0.2.0" in msg and "pipx upgrade webbee" in msg


def test_none_when_current_is_latest(tmp_path):
    assert check_for_update("0.2.0", cache_path=tmp_path / "u.json", now=1000.0,
                            fetch=lambda: "0.2.0") is None


def test_offline_fetch_failure_is_silent(tmp_path):
    assert check_for_update("0.1.0", cache_path=tmp_path / "u.json", now=1000.0,
                            fetch=lambda: None) is None


def test_uses_cache_within_ttl_without_fetching(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text(json.dumps({"latest": "0.5.0", "checked_at": 900.0}))
    calls = []
    msg = check_for_update("0.1.0", cache_path=cache, now=1000.0,
                           fetch=lambda: calls.append(1) or "9.9.9", ttl=86400.0)
    assert calls == []          # cache fresh → no network
    assert "0.5.0" in msg


def test_refetches_after_ttl(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text(json.dumps({"latest": "0.5.0", "checked_at": 0.0}))
    msg = check_for_update("0.1.0", cache_path=cache, now=1_000_000.0,
                           fetch=lambda: "0.6.0", ttl=86400.0)
    assert "0.6.0" in msg


def test_malformed_cache_is_ignored(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text("{not json")
    msg = check_for_update("0.1.0", cache_path=cache, now=1000.0, fetch=lambda: "0.2.0")
    assert "0.2.0" in msg
