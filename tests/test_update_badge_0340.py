"""0.3.40 -- the toolbar's bottom-right version badge is now REAL on every
tab (not just Home): a one-shot background PyPI check at startup feeds
`update_state`, which `tui.pin_version_right` reads on every redraw. And the
version display that used to live INSIDE Home's own content (settings tile +
footer badge, duplicating this and each other) is gone -- covered in
test_terminal_ux_0336.py / test_home_view.py; this file covers the NEW
background-check plumbing itself, offline/pure (fake fetch, tmp cache file).
"""
import asyncio

from webbee.repl import _check_update_bg


def test_check_update_bg_writes_notice_and_checked_true(tmp_path):
    cache = tmp_path / "update.json"
    calls = {"invalidated": 0}
    state: dict = {"notice": "", "checked": None}

    asyncio.run(_check_update_bg(
        state, invalidate=lambda: calls.__setitem__("invalidated", calls["invalidated"] + 1),
        fetch=lambda: "9.9.9", cache_path=cache))

    assert state["checked"] is True
    assert calls["invalidated"] == 1


def test_check_update_bg_offline_leaves_checked_false(tmp_path):
    cache = tmp_path / "update.json"
    state: dict = {"notice": "", "checked": None}

    def _boom():
        raise RuntimeError("no network")

    asyncio.run(_check_update_bg(state, fetch=_boom, cache_path=cache))

    assert state["checked"] is False
    assert state["notice"] == ""


def test_check_update_bg_never_raises_even_with_a_broken_invalidate(tmp_path):
    cache = tmp_path / "update.json"
    state: dict = {"notice": "", "checked": None}

    def _bad_invalidate():
        raise RuntimeError("ui gone")

    asyncio.run(_check_update_bg(state, invalidate=_bad_invalidate,
                                 fetch=lambda: None, cache_path=cache))
    assert state["checked"] is False


def test_check_update_bg_reuses_the_same_cache_file_home_uses(tmp_path):
    """Same TTL cache home.py's own Home-only check reads/writes -- wiring
    the badge into every tab must not double the PyPI traffic when Home
    already warmed the cache this run."""
    cache = tmp_path / "update.json"
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return "1.2.3"

    state1: dict = {"notice": "", "checked": None}
    asyncio.run(_check_update_bg(state1, fetch=_fetch, cache_path=cache))
    assert calls["n"] == 1

    state2: dict = {"notice": "", "checked": None}
    asyncio.run(_check_update_bg(state2, fetch=_fetch, cache_path=cache))
    assert calls["n"] == 1
    assert state2["checked"] is True
