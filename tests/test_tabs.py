"""tab_fragments (W4a Task 4; chip redesign 0.3.24) — the browser tab bar as
a PURE fragment builder, queue_panel discipline: unit-tested without a
prompt_toolkit Application. Home always first, glyph fixed ◆, never
closable; session tabs carry their own switch/close mouse handlers and
middle-truncate their labels to fit a live width. Every tab renders as a
padded CHIP (`" {glyph} {label} "` — one leading + one trailing space baked
into the styled fragment itself), separated by exactly one dim `" │ "`
between each pair (none at the ends); the ACTIVE tab's chip carries
`class:tab.active` (resolved to a solid bg in tui.py's Style dict) and its
`✕` carries `class:tab.close.active` so the whole row reads as one
contiguous block."""
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from webbee.slots import SessionSlot, SlotManager
from webbee.tabs import (TAB_STYLE_ACTIVE, TAB_STYLE_ALERT, TAB_STYLE_CLOSE,
                         TAB_STYLE_CLOSE_ACTIVE, TAB_STYLE_IDLE, TAB_STYLE_SEP,
                         _fit, tab_fragments)


class _FakeSink:
    def __init__(self, consent=False, busy=False):
        self._consent = consent
        self._busy = busy

    def consent_pending(self):
        return self._consent

    def is_busy(self):
        return self._busy


def _mk_slot(kind="session", sink=None, label="t"):
    return SessionSlot(kind=kind, workspace=".", label=label, pane=object(), sink=sink, agent=None)


def _mk_slots(*session_specs, active_idx=1):
    """Home at 0 + one session slot per (label, sink) pair in
    `session_specs`."""
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home", label="Home"))
    for label, sink in session_specs:
        mgr.add(_mk_slot(sink=sink, label=label))
    mgr.active_idx = active_idx
    return mgr


def _text(frags):
    return "".join(f[1] for f in frags)


def _up(handler):
    ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    return handler(ev)


def _scroll(handler):
    ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.SCROLL_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    return handler(ev)


def _noop(*_a, **_kw):
    return None


# ── glyphs, numbering, active accent ────────────────────────────────────────

def test_active_tab_gets_accent_and_active_marker():
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    text = _text(frags)
    styles = {f[1]: f[0] for f in frags}
    assert " ● 1·alpha ○ " in text                      # active session marked ●N, padded chip
    assert " ○ 2·beta ○ " in text                        # inactive session marked ○N, padded chip
    assert styles[" ● 1·alpha ○ "] == TAB_STYLE_ACTIVE
    assert styles[" ○ 2·beta ○ "] == TAB_STYLE_IDLE


def test_home_active_gets_accent_style_but_glyph_and_label_stay_fixed():
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=0)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    home_frag = frags[0]
    assert home_frag[1] == " ◆ Home "
    assert home_frag[0] == TAB_STYLE_ACTIVE


def test_home_inactive_is_idle_style():
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    home_frag = frags[0]
    assert home_frag[1] == " ◆ Home "
    assert home_frag[0] == TAB_STYLE_IDLE


def test_glyphs_come_from_status_glyph_busy_and_idle():
    slots = _mk_slots(("alpha", _FakeSink(busy=True)), ("beta", _FakeSink()), active_idx=1)
    text = _text(tab_fragments(slots, on_switch=_noop, on_close=_noop))
    assert " ● 1·alpha ▶ " in text
    assert " ○ 2·beta ○ " in text


# ── chip padding + separators (0.3.24 redesign) ─────────────────────────────

def test_every_chip_body_has_uniform_single_space_padding():
    # Home + every session body fragment (never the sep/close pieces) starts
    # and ends with EXACTLY one space, not zero and not two.
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    bodies = [f[1] for f in frags if f[0] not in (TAB_STYLE_SEP, TAB_STYLE_CLOSE, TAB_STYLE_CLOSE_ACTIVE)]
    assert bodies == [" ◆ Home ", " ● 1·alpha ○ ", " ○ 2·beta ○ "]
    for body in bodies:
        assert body.startswith(" ") and not body.startswith("  ")
        assert body.endswith(" ") and not body.endswith("  ")


def test_exactly_one_separator_between_each_pair_of_tabs_none_at_ends():
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), ("gamma", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    seps = [f for f in frags if f[0] == TAB_STYLE_SEP]
    assert len(seps) == 3                          # one before EACH session tab
    assert all(f[1] == " │ " for f in seps)
    assert frags[0][0] != TAB_STYLE_SEP             # never at the very start (Home's own chip)
    assert frags[-1][0] != TAB_STYLE_SEP            # never at the very end (last tab's close)


def test_close_fragment_is_uniformly_padded():
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    close = [f[1] for f in frags if f[0] in (TAB_STYLE_CLOSE, TAB_STYLE_CLOSE_ACTIVE)]
    assert close == [" ✕ "]


# ── ⚠ background consent badge ──────────────────────────────────────────────

def test_alert_badge_on_background_consent_tab():
    # alpha is ACTIVE and idle; beta is a BACKGROUND tab awaiting consent —
    # its ⚠ badge must be visible (and styled tab.alert) from alpha's view.
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink(consent=True)), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    styles = {f[1]: f[0] for f in frags}
    assert " ○ 2·beta ⚠ " in styles
    assert styles[" ○ 2·beta ⚠ "] == TAB_STYLE_ALERT


def test_alert_style_never_applied_to_the_active_tab_itself():
    # tab.alert exists ONLY to flag a BACKGROUND tab -- an ACTIVE tab that
    # happens to have a ⚠ (own pending consent) must never get tab.alert,
    # since only the active chip owns a background and alert has none (it
    # would silently lose its highlight).
    slots = _mk_slots(("alpha", _FakeSink(consent=True)), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    styles = {f[1]: f[0] for f in frags}
    assert styles[" ● 1·alpha ⚠ "] == TAB_STYLE_ACTIVE
    assert TAB_STYLE_ALERT not in styles.values()


def test_active_tab_with_consent_pending_stays_active_styled_not_alert():
    # The badge exists to warn about a BACKGROUND tab; the active tab's own
    # consent state is already obvious from the toolbar, so it just stays
    # tab.active (no combined "active + alert" style is defined).
    slots = _mk_slots(("alpha", _FakeSink(consent=True)), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    styles = {f[1]: f[0] for f in frags}
    assert " ● 1·alpha ⚠ " in styles
    assert styles[" ● 1·alpha ⚠ "] == TAB_STYLE_ACTIVE


# ── Home has no close ───────────────────────────────────────────────────────

def test_home_never_gets_a_close_fragment():
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    assert not any(f[1].strip() == "✕" for f in frags[:1])
    # Only ONE fragment renders before the first separator (Home's body) —
    # no trailing close piece tacked onto it.
    assert frags[0][1] == " ◆ Home "
    assert frags[1][1] == " │ "   # separator, not a close glyph


def test_single_slot_home_only_has_no_close_fragment_at_all():
    slots = _mk_slots(active_idx=0)   # no session tabs at all
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    assert not any("✕" in f[1] for f in frags)


# ── mouse dispatch: switch ───────────────────────────────────────────────────

def test_click_on_tab_body_fires_on_switch_with_its_index():
    hits = []
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=hits.append, on_close=_noop)
    home_handler = frags[0][2]
    alpha_handler = frags[2][2]     # frags: [home, sep, alpha, close, sep, beta, close]
    beta_handler = frags[5][2]
    assert _up(home_handler) is None and hits == [0]
    assert _up(alpha_handler) is None and hits == [0, 1]
    assert _up(beta_handler) is None and hits == [0, 1, 2]


def test_switch_handler_wheel_falls_through_notimplemented():
    hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=hits.append, on_close=_noop)
    alpha_handler = frags[2][2]
    assert _scroll(alpha_handler) is NotImplemented
    assert hits == []


# ── mouse dispatch: close ───────────────────────────────────────────────────

def test_session_close_fragment_fires_on_close_with_its_index():
    hits = []
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=hits.append)
    close_alpha = frags[3]     # [home, sep, alpha, CLOSE-alpha, sep, beta, close-beta]
    close_beta = frags[6]
    # alpha (idx 1) IS the active tab -- its ✕ gets the bg-contiguous style;
    # beta (idx 2, background) keeps the plain dim one.
    assert close_alpha[1] == " ✕ " and close_alpha[0] == TAB_STYLE_CLOSE_ACTIVE
    assert close_beta[1] == " ✕ " and close_beta[0] == TAB_STYLE_CLOSE
    assert _up(close_alpha[2]) is None and hits == [1]
    assert _up(close_beta[2]) is None and hits == [1, 2]


def test_close_handler_wheel_falls_through_notimplemented():
    hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=hits.append)
    close_alpha = frags[3]
    assert _scroll(close_alpha[2]) is NotImplemented
    assert hits == []


# ── middle truncation at narrow width ───────────────────────────────────────

def test_fit_is_a_noop_within_the_limit():
    assert _fit("short", 20) == "short"
    assert _fit("", 20) == ""


def test_fit_middle_truncates_and_never_shrinks_below_floor():
    long_label = "workspace-alpha-superlongname"
    fitted = _fit(long_label, 8)
    assert len(fitted) == 8 and "…" in fitted
    assert fitted.startswith(long_label[:4])
    # a smaller max_len still can't go below the 8-char floor
    assert len(_fit(long_label, 2)) == 8


def test_narrow_width_truncates_labels_to_the_floor_and_row_fits():
    # width=52 is chosen so the computed per-label budget lands exactly on
    # the 8-char floor (chip padding raises the fixed per-tab overhead from
    # the pre-0.3.24 shape, so the exact width that lands on the floor
    # shifted too): both long labels truncate to precisely 8 chars and the
    # whole row still fits inside `width` (no floor-forced overflow).
    label1, label2 = "workspace-one-long-name", "workspace-two-long-name"
    slots = _mk_slots((label1, _FakeSink()), (label2, _FakeSink()), active_idx=1)
    width = 52
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop, width=width)
    text = _text(frags)
    assert len(text) <= width
    fitted1, fitted2 = _fit(label1, 8), _fit(label2, 8)
    assert len(fitted1) == 8 and len(fitted2) == 8
    assert fitted1 in text and fitted2 in text
    assert label1 not in text and label2 not in text   # genuinely truncated


def test_width_zero_means_no_truncation_labels_stay_full():
    long_label = "a-very-long-workspace-directory-name"
    slots = _mk_slots((long_label, _FakeSink()), active_idx=1)
    text = _text(tab_fragments(slots, on_switch=_noop, on_close=_noop, width=0))
    assert long_label in text
    assert "…" not in text


# ── renders even with a single slot ─────────────────────────────────────────

def test_single_slot_home_alone_still_renders_the_bar():
    slots = _mk_slots(active_idx=0)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    assert frags
    assert _text(frags) == " ◆ Home "


def test_empty_slot_manager_renders_nothing():
    mgr = SlotManager()
    assert tab_fragments(mgr, on_switch=_noop, on_close=_noop) == []


# ── FIX6: drag first-refusal (a pane selection armed below the tab bar) ────

def test_close_click_with_armed_drag_forward_consumes_event_never_closes():
    switch_hits, close_hits = [], []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    forward = lambda ev: True   # noqa: E731 -- simulates an ARMED drag (pane.forward_mouse)
    frags = tab_fragments(slots, on_switch=switch_hits.append, on_close=close_hits.append,
                          forward=forward)
    close_alpha = frags[3][2]
    assert _up(close_alpha) is None    # consumed by forward -- same "stop here" contract as tui._forwarding
    assert close_hits == []            # NO close fired
    assert switch_hits == []


def test_switch_click_with_armed_drag_forward_consumes_event_never_switches():
    switch_hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    forward = lambda ev: True   # noqa: E731
    frags = tab_fragments(slots, on_switch=switch_hits.append, on_close=_noop, forward=forward)
    alpha_handler = frags[2][2]
    home_handler = frags[0][2]
    assert _up(alpha_handler) is None and switch_hits == []
    assert _up(home_handler) is None and switch_hits == []   # Home's own handler too


def test_close_click_with_unarmed_forward_still_fires_close_as_before():
    # forward=lambda ev: False (drag NOT armed) -- dispatch proceeds exactly
    # like the forward=None (default) case every existing test above covers.
    close_hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    forward = lambda ev: False   # noqa: E731
    frags = tab_fragments(slots, on_switch=_noop, on_close=close_hits.append, forward=forward)
    close_alpha = frags[3][2]
    assert _up(close_alpha) is None
    assert close_hits == [1]


def test_switch_click_with_unarmed_forward_still_fires_switch_as_before():
    switch_hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    forward = lambda ev: False   # noqa: E731
    frags = tab_fragments(slots, on_switch=switch_hits.append, on_close=_noop, forward=forward)
    alpha_handler = frags[2][2]
    assert _up(alpha_handler) is None
    assert switch_hits == [1]
