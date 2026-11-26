"""The tray under the input: one order of zones, one hint at a time, the version from facts.

The owner, 2026-11-26: the badge said "v0.7.18 → 0.7.18 available"; the tray was "a mess" -- it must be tidy,
thought through and fully working; hints one at a time, in turn; the version badge plays short phrases in turn."""
from webbee.home_view import badge_beats, version_badge
from webbee.tui import TOOLBAR_TIPS, build_toolbar, toolbar_tip
from webbee.update import restart_notice, update_facts


def _txt(frags):
    return "".join(t for _, t in frags)


def test_the_restart_notice_no_longer_makes_the_badge_name_the_running_version_twice():
    notice = restart_notice("0.7.18", "0.7.19")              # "... v0.7.19 is installed, ... still runs v0.7.18 ..."
    assert version_badge("0.7.18", notice, installed="0.7.19") == ("v0.7.18 → 0.7.19 · restart", "class:home.update")
    assert "0.7.18 available" not in version_badge("0.7.18", notice)[0]        # and never out of its words
    assert badge_beats("0.7.18", notice, True, installed="0.7.19") == ("v0.7.18", "0.7.19 installed",
                                                                       "restart to use it")


def test_the_badge_plays_short_phrases_in_turn_for_every_state():
    assert badge_beats("0.7.20", "", True) == ("v0.7.20", "up to date")
    assert badge_beats("0.7.20", "n", True, latest="0.7.21") == ("v0.7.20", "0.7.21 is out", "click to upgrade")
    assert badge_beats("0.7.20", "", None) == ("v0.7.20",) == badge_beats("0.7.20", "", False)   # no claim
    for beats in (badge_beats("0.7.20", "", True), badge_beats("0.7.20", "n", True, latest="0.7.21"),
                  badge_beats("0.7.20", "n", True, installed="0.7.21")):
        assert all(len(b) <= 17 for b in beats)                                # short: never one long line


def test_the_freshness_facts_name_the_newer_release_and_are_asked_again_within_the_hour(tmp_path):
    cache = tmp_path / "update.json"
    calls = []

    def fetch():
        calls.append(1)
        return "0.7.21"
    f = update_facts("0.7.20", cache_path=cache, now=1000.0, fetch=fetch)
    assert f["latest"] == "0.7.21" and f["checked"] is True and "0.7.21" in f["notice"]
    update_facts("0.7.20", cache_path=cache, now=1000.0 + 3000, fetch=fetch)
    assert len(calls) == 1                                    # within the hour: the cache answers
    update_facts("0.7.20", cache_path=cache, now=1000.0 + 3700, fetch=fetch)
    assert len(calls) == 2                                    # after it: asked again (it was a whole day)
    assert update_facts("0.7.21", cache_path=cache, now=1000.0 + 3700, fetch=fetch)["latest"] == ""
    assert update_facts("0.7.20", cache_path=tmp_path / "x.json", now=1.0, fetch=lambda: None) == {
        "notice": "", "checked": False, "latest": ""}


def test_who_works_stands_in_the_same_place_whether_a_turn_runs_or_not():
    idle = _txt(build_toolbar("plan", 51000, 66, tier="supersmart"))
    busy = _txt(build_toolbar("plan", 51000, 66, tier="supersmart", busy=True, current="bash › ok", elapsed=75,
                              tools=1))
    who = "mode: plan · model: SuperSmart"
    assert who in idle and who in busy
    assert idle.index(who) <= 2 and busy.index(who) <= 4                     # first in the row, after the spinner
    assert "1:15 · 1 action · 51k tok · 66 credits" in busy                   # minutes, a unit, the spend too
    assert "   ·   bash › ok   ·   " in busy                                  # groups stand apart the same way


def test_one_hint_at_a_time_in_turn_and_only_when_it_fits_whole():
    assert len(set(TOOLBAR_TIPS)) == len(TOOLBAR_TIPS) >= 6
    seen = [toolbar_tip(now=i * 8.0) for i in range(len(TOOLBAR_TIPS))]
    assert seen == list(TOOLBAR_TIPS) and toolbar_tip(now=3.0) == toolbar_tip(now=7.9)
    row = _txt(build_toolbar("default", 0, 0, width=200))
    assert row.count("Tip: ") == 1 and sum(t in row for t in TOOLBAR_TIPS) == 1
    assert "Tip:" not in _txt(build_toolbar("default", 0, 0, width=60))


def test_a_narrow_terminal_loses_whole_things_in_order_and_the_row_never_runs_past_its_width():
    def row(width):
        return _txt(build_toolbar("autopilot", 128000, 3400, tier="ultrasmart", busy=True, elapsed=312, tools=14,
                                  current="bash › tests/test_tools.py ........ a long line of output", width=width))
    wide = row(200)
    assert "Esc to stop" in wide and "3.4k credits" in wide and "a long line of output" in wide
    kept = []
    for width in range(200, 64, -1):
        r = row(width)
        assert len(r) <= width, (width, len(r), r)
        assert "mode: autopilot · model: UltraSmart" in r and "5:12" in r      # who and how long always stay
        kept.append(("Esc to stop" in r, "a long line of output" in r, "3.4k credits" in r, "14 actions" in r))
    # the order things go in, each whole: the hint, then the action's text is cut, then the spend, then the count
    for col in range(4):
        flags = [k[col] for k in kept]
        assert flags == sorted(flags, reverse=True)                             # once gone, never back
    first_gone = [next((i for i, k in enumerate(kept) if not k[col]), len(kept)) for col in range(4)]
    assert first_gone == sorted(first_gone) and first_gone[0] < first_gone[2] < first_gone[3] < len(kept)
    cut = next(r for r in (row(w) for w in range(200, 64, -1)) if "a long line of output" not in r)
    assert "…" in cut                                                           # a cut is marked


def test_the_idle_row_keeps_its_figures_on_a_narrow_terminal_and_drops_the_words():
    def row(width):
        return _txt(build_toolbar("default", 184320, 412, tier="supersmart", width=width))
    assert "184.3k tok · 412 credits this session" in row(120)
    assert "184.3k tok · 412 credits" in row(70) and "this session" not in row(70)
    assert "184.3k tok" in row(52) and "credits" not in row(52)
    for width in range(120, 51, -1):
        assert len(row(width)) <= width, (width, row(width))
