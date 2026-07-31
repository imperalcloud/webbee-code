"""0.3.37 — the version badge is pinned to the bottom-right corner of the
window (the toolbar is the last row of the root HSplit, so its right edge IS
that corner). These drive the PURE builders directly: no Application needed."""
from webbee.tui import build_toolbar, pin_version_right, version_badge_text


def _text(frags):
    return "".join(t for _, t in frags)


def test_badge_lands_flush_at_the_right_edge():
    frags = [("class:tb.dim", "  mode: default")]
    out = pin_version_right(frags, "0.3.37", 60)
    assert len(_text(out)) == 60            # exactly fills the row
    assert _text(out).endswith(" v0.3.37 ")  # ...and the badge is the last thing on it


def test_badge_is_a_separate_styled_fragment():
    out = pin_version_right([("class:tb.dim", "x")], "0.3.37", 40)
    assert out[-1][0] == "class:tb.version"
    assert out[-1][1] == " v0.3.37 "


def test_unknown_width_is_left_untouched():
    frags = [("class:tb.dim", "x")]
    assert pin_version_right(frags, "0.3.37", 0) == frags
    assert pin_version_right(frags, "0.3.37", -1) == frags


def test_badge_dropped_rather_than_truncating_real_content():
    frags = [("class:tb.dim", "x" * 39)]
    out = pin_version_right(frags, "0.3.37", 40)   # no room for badge + content
    assert out == frags
    assert _text(out) == "x" * 39                  # data survives intact


def test_every_toolbar_state_can_carry_the_badge():
    """idle / busy / consent / reconnecting all end flush-right with the badge --
    it does not belong to one branch, it belongs to the WINDOW.

    Models `_toolbar`'s real contract: the badge's columns are RESERVED first
    (build_toolbar fits its hints into what is left), then the badge is pinned
    into the reserved space. Without the reservation the hint text eats the
    whole row and the badge blinks out -- the exact bug this guards."""
    W = 100
    fit = W - len(version_badge_text("0.3.37"))
    states = [
        build_toolbar("default", 1, 2, width=fit),
        build_toolbar("default", 1, 2, busy=True, current="thinking", width=fit),
        build_toolbar("default", 1, 2, consent=True, width=fit),
        build_toolbar("default", 1, 2, busy=True, reconnecting=2, width=fit),
    ]
    for frags in states:
        out = pin_version_right(frags, "0.3.37", W)
        assert _text(out).endswith(" v0.3.37 "), _text(out)
        assert len(_text(out)) == W


def test_reservation_keeps_the_badge_on_a_narrow_window():
    """90 columns: the hints degrade (by design), the badge still lands. (90,
    not 80, since webbee-code-model-selector-always-visible-v1 made the base
    data line -- mode + the now-permanent model indicator + spend -- longer.)"""
    W = 90
    fit = W - len(version_badge_text("0.3.37"))
    out = pin_version_right(build_toolbar("default", 1, 2, width=fit), "0.3.37", W)
    assert _text(out).endswith(" v0.3.37 ")
    assert len(_text(out)) == W


def test_badge_text_is_single_source_of_truth():
    assert version_badge_text("9.9.9") == " v9.9.9 "
    out = pin_version_right([("class:tb.dim", "x")], "9.9.9", 40)
    assert out[-1][1] == version_badge_text("9.9.9")


# --------------------------------------------------------------------------
# 0.3.40 — the badge is now LIVE-CHECKED (checked/notice) on every tab, and
# a click on it flashes the upgrade hint. These mirror home_view.version_badge
# exactly (same wording/colour), just drawn in the toolbar's corner instead.
# --------------------------------------------------------------------------
def test_unchecked_state_is_the_old_bare_badge():
    """checked=None (the default, and the pre-0.3.40 behaviour): no freshness
    claim at all -- same plain `v<version>` in the quiet tb.version class."""
    out = pin_version_right([("class:tb.dim", "x")], "0.3.40", 40)
    assert out[-1] == ("class:tb.version", " v0.3.40 ")


def test_checked_up_to_date_uses_the_fresh_style():
    out = pin_version_right([("class:tb.dim", "x")], "0.3.40", 40,
                            notice="", checked=True)
    assert out[-1][0] == "class:tb.fresh"
    assert out[-1][1] == " v0.3.40 · up to date "


def test_checked_update_available_uses_the_update_style():
    out = pin_version_right([("class:tb.dim", "x")], "0.3.39", 60,
                            notice="🐝 webbee v0.3.40 available — upgrade: pipx upgrade webbee",
                            checked=True)
    assert out[-1][0] == "class:tb.update"
    assert out[-1][1] == " v0.3.39 → 0.3.40 available "


def test_checked_false_offline_is_the_bare_badge_no_false_claim():
    out = pin_version_right([("class:tb.dim", "x")], "0.3.40", 40,
                            notice="", checked=False)
    assert out[-1] == ("class:tb.version", " v0.3.40 ")


def test_badge_click_flashes_the_notice_on_mouse_up():
    from webbee.tui import _badge_click
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton

    class _FakePane:
        def __init__(self):
            self.flashed = None

        def flash_note(self, msg, secs=4.0):
            self.flashed = (msg, secs)

    pane = _FakePane()
    handler = _badge_click(pane, "upgrade: pipx upgrade webbee", forward=lambda ev: False)
    ev = MouseEvent(position=None, event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(ev) is None
    assert pane.flashed == ("upgrade: pipx upgrade webbee", 6.0)


def test_badge_click_yields_to_a_forwarded_drag():
    """A drag-release forwarded from the output pane wins -- the click never
    fires (mirrors _forwarding's own contract, just for the badge fragment)."""
    from webbee.tui import _badge_click
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton

    calls = []

    class _FakePane:
        def flash_note(self, msg, secs=4.0):
            calls.append(msg)

    handler = _badge_click(_FakePane(), "some notice", forward=lambda ev: True)
    ev = MouseEvent(position=None, event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(ev) is None
    assert calls == []


def test_badge_click_ignores_non_click_events():
    from webbee.tui import _badge_click
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton

    handler = _badge_click(object(), "notice", forward=lambda ev: False)
    ev = MouseEvent(position=None, event_type=MouseEventType.MOUSE_DOWN,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(ev) is NotImplemented
