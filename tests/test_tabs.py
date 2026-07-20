"""tab_fragments (W4a Task 4; chip redesign 0.3.24; precise hit-zones + the
"+" new-tab chip + busy-close-confirm, 0.3.25) — the browser tab bar as a
PURE fragment builder, queue_panel discipline: unit-tested without a
prompt_toolkit Application. Home always first, glyph fixed ◆, never
closable; session tabs carry their own switch/close mouse handlers and
middle-truncate their labels to fit a live width. Every tab renders as a
padded CHIP (`" {glyph} {label} "` — one leading + one trailing space baked
into the styled fragment itself), separated by exactly one dim `" │ "`
between each pair (and once more before the trailing + chip); the ACTIVE
tab's chip carries `class:tab.active` (resolved to a solid bg in tui.py's
Style dict) and its ✕ carries `class:tab.close.active` so the whole row
reads as one contiguous block.

0.3.25: the ✕ (and the + chip) are each three fragments -- an unclickable
pad, the bare glyph (the ONLY one carrying a mouse handler), another
unclickable pad -- so a near-miss click on the padding does nothing. A
busy tab's ✕ renders "✕?" (armed, `class:tab.alert`) instead of closing on
the first click; helpers below locate a session tab's own close control by
anchoring off its (always-unique) body text rather than a hardcoded
fragment index, since the exact index shifts with the row's shape."""
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from webbee.slots import SessionSlot, SlotManager
from webbee.tabs import (TAB_STYLE_ACTIVE, TAB_STYLE_ALERT, TAB_STYLE_CLOSE,
                         TAB_STYLE_CLOSE_ACTIVE, TAB_STYLE_IDLE, TAB_STYLE_NEW,
                         TAB_STYLE_SEP, _fit, tab_fragments)


class _FakeSink:
    def __init__(self, consent=False, busy=False):
        self._consent = consent
        self._busy = busy

    def consent_pending(self):
        return self._consent

    def is_busy(self):
        return self._busy


def _mk_slot(kind="session", sink=None, label="t", close_armed=False):
    slot = SessionSlot(kind=kind, workspace=".", label=label, pane=object(), sink=sink, agent=None)
    slot.close_armed = close_armed
    return slot


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


def _glyph_after_body(frags, body_text):
    """The close control's GLYPH fragment (the middle one of its pad/glyph/
    pad triple) that immediately follows the session tab whose body text is
    `body_text` -- anchored off the body's own (always-unique) text instead
    of a hardcoded absolute index, since the exact index shifts with
    however many OTHER tabs/pads precede it."""
    for i, f in enumerate(frags):
        if len(f) == 3 and f[1] == body_text:
            return frags[i + 2]
    raise AssertionError(f"body {body_text!r} not found in {frags!r}")


def _new_chip_pieces(frags):
    """The trailing + chip's own (pad, glyph, pad) triple -- always the last
    three fragments, unconditionally rendered."""
    return frags[-3], frags[-2], frags[-1]


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
    # Home + every session body fragment (never a sep/pad/close/new piece)
    # starts and ends with EXACTLY one space, not zero and not two.
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    excluded_styles = (TAB_STYLE_SEP, TAB_STYLE_CLOSE, TAB_STYLE_CLOSE_ACTIVE,
                       TAB_STYLE_ALERT, TAB_STYLE_NEW)
    bodies = [f[1] for f in frags if f[0] not in excluded_styles]
    assert bodies == [" ◆ Home ", " ● 1·alpha ○ ", " ○ 2·beta ○ "]
    for body in bodies:
        assert body.startswith(" ") and not body.startswith("  ")
        assert body.endswith(" ") and not body.endswith("  ")


def test_exactly_one_separator_before_each_session_tab_and_the_new_chip():
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), ("gamma", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    seps = [f for f in frags if f[0] == TAB_STYLE_SEP]
    assert len(seps) == 4                           # one before EACH session tab + one before +
    assert all(f[1] == " │ " for f in seps)
    assert frags[0][0] != TAB_STYLE_SEP              # never at the very start (Home's own chip)
    assert frags[-1][0] != TAB_STYLE_SEP             # never at the very end (the + chip's own pad)


def test_close_control_is_pad_glyph_pad_three_fragments():
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    glyph = _glyph_after_body(frags, " ● 1·alpha ○ ")
    idx = frags.index(glyph)
    pad_before, pad_after = frags[idx - 1], frags[idx + 1]
    assert glyph[1] == "✕"
    assert len(glyph) == 3                          # the ONLY one with a handler
    assert pad_before[1] == " " and len(pad_before) == 2   # bare 2-tuple -- no handler at all
    assert pad_after[1] == " " and len(pad_after) == 2


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
    # tab.alert exists to flag a BACKGROUND tab (or an armed busy-close ✕);
    # an ACTIVE tab that happens to have a ⚠ (own pending consent) must
    # never get tab.alert on its BODY chip, since only the active chip owns
    # a background and alert has none (it would silently lose its highlight).
    slots = _mk_slots(("alpha", _FakeSink(consent=True)), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    styles = {f[1]: f[0] for f in frags}
    assert styles[" ● 1·alpha ⚠ "] == TAB_STYLE_ACTIVE


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
    styles = {f[1]: f for f in frags}
    alpha_handler = styles[" ● 1·alpha ○ "][2]
    beta_handler = styles[" ○ 2·beta ○ "][2]
    assert _up(home_handler) is None and hits == [0]
    assert _up(alpha_handler) is None and hits == [0, 1]
    assert _up(beta_handler) is None and hits == [0, 1, 2]


def test_switch_handler_wheel_falls_through_notimplemented():
    hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=hits.append, on_close=_noop)
    styles = {f[1]: f for f in frags}
    alpha_handler = styles[" ● 1·alpha ○ "][2]
    assert _scroll(alpha_handler) is NotImplemented
    assert hits == []


# ── mouse dispatch: close ───────────────────────────────────────────────────

def test_session_close_fragment_fires_on_close_with_its_index():
    hits = []
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=hits.append)
    # alpha (idx 1) IS the active tab -- its ✕ gets the bg-contiguous style;
    # beta (idx 2, background) keeps the plain dim one.
    close_alpha = _glyph_after_body(frags, " ● 1·alpha ○ ")
    close_beta = _glyph_after_body(frags, " ○ 2·beta ○ ")
    assert close_alpha[1] == "✕" and close_alpha[0] == TAB_STYLE_CLOSE_ACTIVE
    assert close_beta[1] == "✕" and close_beta[0] == TAB_STYLE_CLOSE
    assert _up(close_alpha[2]) is None and hits == [1]
    assert _up(close_beta[2]) is None and hits == [1, 2]


def test_close_handler_wheel_falls_through_notimplemented():
    hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=hits.append)
    close_alpha = _glyph_after_body(frags, " ● 1·alpha ○ ")
    assert _scroll(close_alpha[2]) is NotImplemented
    assert hits == []


def test_close_pad_fragments_carry_no_handler_at_all():
    # 0.3.25 precise hit-zones: a click landing on the PADDING beside the ✕
    # cannot fire on_close at all -- there is no handler to invoke, unlike
    # the old merged " ✕ " fragment where the whole run shared one.
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    glyph = _glyph_after_body(frags, " ● 1·alpha ○ ")
    idx = frags.index(glyph)
    pad_before, pad_after = frags[idx - 1], frags[idx + 1]
    assert len(pad_before) == 2 and len(pad_after) == 2


# ── busy-close confirm (Part D): armed tab renders "✕?" ─────────────────────

def test_armed_close_renders_question_mark_glyph_in_alert_style():
    sink = _FakeSink()
    slot = _mk_slot(sink=sink, label="alpha", close_armed=True)
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home", label="Home"))
    mgr.add(slot)
    mgr.active_idx = 1
    frags = tab_fragments(mgr, on_switch=_noop, on_close=_noop)
    glyph = _glyph_after_body(frags, " ● 1·alpha ○ ")
    assert glyph[1] == "✕?"
    assert glyph[0] == TAB_STYLE_ALERT


def test_unarmed_close_stays_the_plain_glyph():
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    glyph = _glyph_after_body(frags, " ● 1·alpha ○ ")
    assert glyph[1] == "✕"


def test_armed_close_still_fires_on_close_when_clicked():
    # tab_fragments only RENDERS the armed state -- the arm/close DECISION
    # itself lives in the caller (tui._close_tab_click); a click on the
    # rendered "✕?" still calls on_close(idx) exactly like a plain "✕" would.
    hits = []
    sink = _FakeSink()
    slot = _mk_slot(sink=sink, label="alpha", close_armed=True)
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home", label="Home"))
    mgr.add(slot)
    mgr.active_idx = 1
    frags = tab_fragments(mgr, on_switch=_noop, on_close=hits.append)
    glyph = _glyph_after_body(frags, " ● 1·alpha ○ ")
    assert _up(glyph[2]) is None and hits == [1]


# ── the trailing + chip (0.3.25) ─────────────────────────────────────────────

def test_new_chip_always_renders_even_with_home_alone():
    slots = _mk_slots(active_idx=0)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    pad_before, glyph, pad_after = _new_chip_pieces(frags)
    assert glyph[1] == "+" and glyph[0] == TAB_STYLE_NEW
    assert len(pad_before) == 2 and len(pad_after) == 2
    # preceded by exactly one separator, right after Home's own chip
    assert frags[-4][1] == " │ " and frags[-4][0] == TAB_STYLE_SEP


def test_new_chip_renders_after_every_session_tab_too():
    slots = _mk_slots(("alpha", _FakeSink()), ("beta", _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)
    _pad_before, glyph, _pad_after = _new_chip_pieces(frags)
    assert glyph[1] == "+"


def test_new_chip_click_fires_on_new_with_no_args():
    hits = []
    slots = _mk_slots(active_idx=0)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop, on_new=lambda: hits.append(1))
    _pad_before, glyph, _pad_after = _new_chip_pieces(frags)
    assert _up(glyph[2]) is None
    assert hits == [1]


def test_new_chip_wheel_falls_through_notimplemented():
    slots = _mk_slots(active_idx=0)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop, on_new=lambda: None)
    _pad_before, glyph, _pad_after = _new_chip_pieces(frags)
    assert _scroll(glyph[2]) is NotImplemented


def test_new_chip_click_with_no_on_new_wired_is_a_harmless_noop():
    slots = _mk_slots(active_idx=0)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop)   # on_new=None default
    _pad_before, glyph, _pad_after = _new_chip_pieces(frags)
    assert _up(glyph[2]) is None   # consumed, never raises


def test_new_chip_pad_fragments_carry_no_handler():
    slots = _mk_slots(active_idx=0)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop, on_new=lambda: None)
    pad_before, _glyph, pad_after = _new_chip_pieces(frags)
    assert len(pad_before) == 2 and len(pad_after) == 2


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


def test_very_narrow_width_truncates_labels_to_the_floor():
    # A width tight enough that the computed per-label budget must land on
    # (or below) the 8-char floor -- both long labels truncate to exactly
    # 8 chars regardless of the exact fixed overhead (Home text, seps,
    # close controls, the reserved + chip), which is deliberately NOT
    # hardcoded here -- it shifts whenever the row's own fixed furniture
    # changes, and re-deriving the EXACT breakpoint each time is brittle.
    label1, label2 = "workspace-one-long-name", "workspace-two-long-name"
    slots = _mk_slots((label1, _FakeSink()), (label2, _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop, width=30)
    text = _text(frags)
    fitted1, fitted2 = _fit(label1, 8), _fit(label2, 8)
    assert len(fitted1) == 8 and len(fitted2) == 8
    assert fitted1 in text and fitted2 in text
    assert label1 not in text and label2 not in text   # genuinely truncated


def test_generous_width_leaves_labels_full():
    label1, label2 = "workspace-one-long-name", "workspace-two-long-name"
    slots = _mk_slots((label1, _FakeSink()), (label2, _FakeSink()), active_idx=1)
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop, width=200)
    text = _text(frags)
    assert label1 in text and label2 in text


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
    text = _text(frags)
    assert text.startswith(" ◆ Home ")
    assert text.rstrip().endswith("+")   # the ALWAYS-present + chip trails Home


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
    close_alpha = _glyph_after_body(frags, " ● 1·alpha ○ ")
    assert _up(close_alpha[2]) is None    # consumed by forward -- same "stop here" contract as tui._forwarding
    assert close_hits == []            # NO close fired
    assert switch_hits == []


def test_switch_click_with_armed_drag_forward_consumes_event_never_switches():
    switch_hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    forward = lambda ev: True   # noqa: E731
    frags = tab_fragments(slots, on_switch=switch_hits.append, on_close=_noop, forward=forward)
    styles = {f[1]: f for f in frags}
    alpha_handler = styles[" ● 1·alpha ○ "][2]
    home_handler = frags[0][2]
    assert _up(alpha_handler) is None and switch_hits == []
    assert _up(home_handler) is None and switch_hits == []   # Home's own handler too


def test_new_chip_click_with_armed_drag_forward_consumes_event_never_fires():
    hits = []
    slots = _mk_slots(active_idx=0)
    forward = lambda ev: True   # noqa: E731
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop,
                          on_new=lambda: hits.append(1), forward=forward)
    _pad_before, glyph, _pad_after = _new_chip_pieces(frags)
    assert _up(glyph[2]) is None
    assert hits == []


def test_close_click_with_unarmed_forward_still_fires_close_as_before():
    # forward=lambda ev: False (drag NOT armed) -- dispatch proceeds exactly
    # like the forward=None (default) case every existing test above covers.
    close_hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    forward = lambda ev: False   # noqa: E731
    frags = tab_fragments(slots, on_switch=_noop, on_close=close_hits.append, forward=forward)
    close_alpha = _glyph_after_body(frags, " ● 1·alpha ○ ")
    assert _up(close_alpha[2]) is None
    assert close_hits == [1]


def test_switch_click_with_unarmed_forward_still_fires_switch_as_before():
    switch_hits = []
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    forward = lambda ev: False   # noqa: E731
    frags = tab_fragments(slots, on_switch=switch_hits.append, on_close=_noop, forward=forward)
    styles = {f[1]: f for f in frags}
    alpha_handler = styles[" ● 1·alpha ○ "][2]
    assert _up(alpha_handler) is None
    assert switch_hits == [1]
