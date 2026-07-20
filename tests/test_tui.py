import asyncio
import re
from types import SimpleNamespace

from webbee.tui import next_mode, build_toolbar


def mk_slots(*, pane=None, sink=None, pending=None, turn=None, mode="default",
            agent=None, workspace=".", label="test", **extra):
    """Task 3 test helper: builds a ONE-session-slot SlotManager so a
    run_session harness test can pass `slots=` instead of the individual
    pane/sink/pending/turn/status/is_busy/... params the OLD signature
    exposed directly. Single-slot behavior is the parity oracle for this
    conversion — every assertion in the converted tests stays untouched.
    `sink` is whatever attribute-bag a test built (a `SimpleNamespace` with
    `status`/`is_busy`/`consent_pending`/`resolve_consent`/`remote_pending`/
    `current_todos` as attributes, or `None` for a sink-less/Home slot) —
    tui reads every one of them via getattr, so a plain namespace works.
    `**extra` sets any other SessionSlot field directly (`qp_ui`, `tp_ui`,
    `pulled`, `history`, ...) for the handful of tests that need one."""
    from webbee.slots import SessionSlot, SlotManager
    slot = SessionSlot(kind="session", workspace=workspace, label=label,
                       pane=pane, sink=sink, agent=agent, mode=mode)
    if pending is not None:
        slot.pending = pending
    if turn is not None:
        slot.turn = turn
    for k, v in extra.items():
        setattr(slot, k, v)
    sm = SlotManager()
    sm.add(slot)
    return sm

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _txt(frags):
    """Join prompt_toolkit formatted-text fragments into the visible string."""
    return "".join(seg for _, seg in frags)


def strip_ansi(s):
    """Strip SGR colour escapes — same pattern OutputPane._plain_lines uses."""
    return _SGR.sub("", s)


def ring_invariant(pane):
    """W2 final-review: the buffer holds lines that precede the ring's first
    record (deque eviction + trims) — `pane._ring_base_lines` is the count
    of those. This must ALWAYS hold: total buffer lines == 1 (the trailing
    split artifact — Rich console.print always ends with a newline) +
    base lines + the sum of every retained record's own line count. Call
    this after any operation that touches the ring, the base, or a trim."""
    assert len(pane._all_lines()) == 1 + pane._ring_base_lines + sum(pane._record_lines)


def test_next_mode_cycles():
    assert next_mode("default") == "plan"
    assert next_mode("plan") == "autopilot"
    assert next_mode("autopilot") == "default"

def test_next_mode_unknown_resets():
    assert next_mode("weird") == "default"

def test_toolbar_idle_has_mode_tokens_cost_and_hint():
    t = _txt(build_toolbar("plan", 51000, 66))
    assert "plan" in t
    assert "51k" in t
    assert "66 credits" in t
    assert "Shift + TAB" in t          # spelled in words, no glyph
    assert "⇧⇥" not in t     # the ⇧⇥ glyph must NOT appear
    assert not NO_CYRILLIC.search(t)


def test_toolbar_humanizes_millions_and_credits():
    # Big numbers stay readable: tokens + credits both scale to k/M/B.
    t = _txt(build_toolbar("default", 1_500_000, 2_000_000))
    assert "1.5M tok" in t
    assert "2M credits" in t


def test_fmt_tokens_scales_k_m_b():
    from webbee.render import _fmt_tokens
    assert _fmt_tokens(900) == "900"
    assert _fmt_tokens(2100) == "2.1k"
    assert _fmt_tokens(51000) == "51k"
    assert _fmt_tokens(1_500_000) == "1.5M"
    assert _fmt_tokens(2_000_000) == "2M"
    assert _fmt_tokens(3_200_000_000) == "3.2B"


def test_toolbar_busy_state_shows_working_dot_and_stop_hint():
    t = _txt(build_toolbar("default", 1200, 0.0143, busy=True,
                           current="notes·delete_note", elapsed=4, tools=3))
    assert "working" in t and "notes·delete_note" in t
    assert "Esc/Ctrl-C to stop" in t and "4s" in t
    assert not NO_CYRILLIC.search(t)


def test_toolbar_consent_state():
    t = _txt(build_toolbar("default", 0, 0.0, consent=True))
    assert "approve?" in t and "Enter to send" in t
    assert not NO_CYRILLIC.search(t)


def test_toolbar_busy_spinner_animates_with_elapsed():
    a = _txt(build_toolbar("default", 0, 0.0, busy=True, elapsed=0.0))
    b = _txt(build_toolbar("default", 0, 0.0, busy=True, elapsed=0.4))
    assert a != b                       # the spinner frame advances as time passes
    assert "working" in a and "working" in b


def test_toolbar_mode_colored_per_mode():
    # each mode carries its own style class so the eye catches the mode at a glance
    def _mode_style(mode):
        for style, seg in build_toolbar(mode, 0, 0.0):
            if seg == mode:
                return style
        return ""
    assert _mode_style("autopilot") == "class:tb.mode.autopilot"
    assert _mode_style("plan") == "class:tb.mode.plan"
    assert _mode_style("default") == "class:tb.mode.default"


def test_output_pane_captures_colored_text():
    # The pane's Rich Console renders into a buffer as ANSI (colours preserved
    # for the FormattedTextControl to show); construction works headlessly.
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.console.print("[bold red]Error[/] ok")
    out = pane.dump()
    assert "Error" in out and "ok" in out
    assert "\x1b[" in out          # ANSI colour escapes preserved for the pane


# ── copy-on-select (drag → OSC 52) ────────────────────────────────────────────

def test_selected_text_single_line():
    # _selected_text now takes ABSOLUTE (line, col) pairs directly (W2 front-3a:
    # the caller resolves viewport + _offset once, at press/move/release time).
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.console.print("hello world")
    assert pane._selected_text((0, 6), (0, 10)) == "world"


def test_selected_text_multi_line_strips_ansi():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.console.print("abcdef")
    pane.console.print("[bold]ghijkl[/]")   # coloured — must be stripped
    pane.console.print("mnopqr")
    assert pane._selected_text((0, 3), (2, 2)) == "def\nghijkl\nmno"


def test_selected_text_reversed_order_normalizes():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.console.print("hello")
    assert pane._selected_text((0, 4), (0, 0)) == "hello"


def test_copy_flash_expires():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.copy_flash = "✓ copied 5 chars"
    pane._flash_until = 0.0            # already in the past
    assert pane.flash() == ""


def test_selection_survives_scroll_between_press_and_release(monkeypatch):
    # W2 front-3a correctness base: the drag anchor is captured ABSOLUTE at
    # MOUSE_DOWN and never re-derived from a later (scrolled) offset. Press
    # at viewport (row=2, col=1) with offset=10 (abs line 12); scroll +5
    # mid-drag (offset becomes 15); release at viewport (row=3, col=4)
    # (abs line 18). Under the OLD viewport-anchor math (re-adding the
    # CURRENT offset to a stale viewport row at MOVE/UP time) the start
    # would have drifted to abs line 17 (2 + 15) instead of staying pinned
    # at 12 — corrupting both the highlight and the copied text.
    import webbee.clipboard as clipboard
    from webbee.tui import OutputPane
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    captured = {}
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: captured.setdefault("text", text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(40)))   # numbered transcript
    pane._offset = 10

    down = MouseEvent(position=Point(1, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)

    pane.scroll(5)                      # scroll mid-drag: offset 10 → 15

    move = MouseEvent(position=Point(4, 3), event_type=MouseEventType.MOUSE_MOVE,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)

    up = MouseEvent(position=Point(4, 3), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(up)

    # abs lines 12..18 exactly (line12[1:] .. line18[:5]) — never line17..
    assert captured["text"] == "ine12\nline13\nline14\nline15\nline16\nline17\nline1"


# ── W2 Task 7: edge-triggered drag auto-scroll + repeating edge tick ─────────

def test_drag_at_bottom_edge_scrolls_and_grows_selection():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))   # 100 lines
    pane._offset = 0

    down = MouseEvent(position=Point(0, 5), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)

    move = MouseEvent(position=Point(3, 9), event_type=MouseEventType.MOUSE_MOVE,     # bottom row
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)
    assert pane._offset == 3 and pane._edge_drag == 1

    # the pointer sits still at the edge — no more MOUSE_MOVE arrives, but the
    # ticker's edge_tick() must keep scrolling AND keep growing the selection.
    pane.edge_tick()
    assert pane._offset == 6
    assert pane._sel[1][0] == pane._offset + pane._view_h - 1

    pane.edge_tick()
    assert pane._offset == 9
    assert pane._sel[1][0] == pane._offset + pane._view_h - 1

    up = MouseEvent(position=Point(3, 9), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(up)
    assert pane._edge_drag == 0


def test_edge_drag_resets_on_mouse_up_and_top_edge_mirrors():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 0

    down = MouseEvent(position=Point(0, 5), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)
    move_bottom = MouseEvent(position=Point(3, 9), event_type=MouseEventType.MOUSE_MOVE,
                             button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move_bottom)
    assert pane._edge_drag == 1

    up = MouseEvent(position=Point(3, 9), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(up)
    assert pane._edge_drag == 0        # MOUSE_UP resets the armed edge

    # --- fresh drag, mirrored at the TOP edge ---
    pane._offset = 20
    down2 = MouseEvent(position=Point(0, 5), event_type=MouseEventType.MOUSE_DOWN,
                       button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down2)
    move_top = MouseEvent(position=Point(3, 0), event_type=MouseEventType.MOUSE_MOVE,   # top row
                          button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move_top)
    assert pane._offset == 17 and pane._edge_drag == -1

    pane.edge_tick()
    assert pane._offset == 14
    assert pane._sel[1][0] == pane._offset

    pane.edge_tick()
    assert pane._offset == 11
    assert pane._sel[1][0] == pane._offset


def test_edge_tick_noop_when_not_dragging():
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 20

    pane._edge_drag = 0                # not armed at all
    pane.edge_tick()
    assert pane._offset == 20

    pane._edge_drag = 1                # armed flag alone isn't enough — needs a live drag too
    pane.control._down_abs = None
    pane.edge_tick()
    assert pane._offset == 20


def test_edge_drag_scroll_clamps_at_buffer_end():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(15)))   # max_off == 5
    pane._offset = 5                                            # already at the bottom

    down = MouseEvent(position=Point(0, 5), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)
    move = MouseEvent(position=Point(3, 9), event_type=MouseEventType.MOUSE_MOVE,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)
    assert pane._offset == 5           # scroll(+3) clamps at max_off — free from pane.scroll

    pane.edge_tick()
    assert pane._offset == 5           # edge_tick's scroll clamps too


# ── W2 final-review Fix 4: click-vs-drag on ABSOLUTE coords, not viewport
# ones — an edge auto-scroll during the drag can put the release on the SAME
# viewport cell the press used while the underlying content has moved; the
# old viewport-only compare mistook that for a click and dropped the copy. ─

def test_mouse_up_same_viewport_cell_after_autoscroll_still_copies(monkeypatch):
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    captured = {}
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: captured.setdefault("text", text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 0

    down = MouseEvent(position=Point(5, 9), event_type=MouseEventType.MOUSE_DOWN,   # bottom row
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)

    move = MouseEvent(position=Point(5, 9), event_type=MouseEventType.MOUSE_MOVE,   # SAME cell
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)                  # bottom edge -> auto-scroll +3
    assert pane._offset == 3

    up = MouseEvent(position=Point(5, 9), event_type=MouseEventType.MOUSE_UP,       # SAME cell as press
                    button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(up)

    assert "text" in captured           # absolute endpoints differ (offset moved) -> copy fires


def test_mouse_up_same_viewport_and_absolute_cell_still_skips_copy_as_a_click(monkeypatch):
    # The other half of Fix 4: a genuine click (no scroll happened in
    # between) still has IDENTICAL absolute endpoints, so it must still be
    # treated as a click, not a drag — the fix only changes WHAT is
    # compared, not the click-suppresses-copy behavior itself.
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    captured = {}
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: captured.setdefault("text", text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 0

    down = MouseEvent(position=Point(3, 4), event_type=MouseEventType.MOUSE_DOWN,   # mid-viewport
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)
    up = MouseEvent(position=Point(3, 4), event_type=MouseEventType.MOUSE_UP,       # no move in between
                    button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(up)

    assert captured == {}                # no scroll happened -> absolute endpoints match -> a click


# ── W2 Task 8: selection capture — neighbor windows forward drag/release ────
# prompt_toolkit has NO mouse capture (events route by pointer POSITION, not
# by who owns a drag): releasing below the output pane used to leave the
# highlight stuck and the copy never fired. `OutputPane.forward_mouse` lets a
# neighbor window (queue/todo panel, toolbar) hand a MOUSE_MOVE/MOUSE_UP back
# to the pane FIRST, while a drag is armed.

def test_forward_mouse_noop_when_no_drag_armed():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    assert pane.control._down_abs is None
    up = MouseEvent(position=Point(4, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    move = MouseEvent(position=Point(1, 0), event_type=MouseEventType.MOUSE_MOVE,
                      button=MouseButton.LEFT, modifiers=frozenset())
    assert pane.forward_mouse(up) is False
    assert pane.forward_mouse(move) is False


def test_forward_mouse_move_extends_selection_to_bottom_row_and_arms_edge_drag():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 20

    down = MouseEvent(position=Point(1, 3), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                 # arms the drag INSIDE the pane (Point is x,y)

    # A neighbor window's own coordinate space (e.g. row 2 of the queue
    # panel) — forward_mouse must ignore ev.position.y entirely and clamp to
    # the pane's own bottom row instead.
    move = MouseEvent(position=Point(7, 2), event_type=MouseEventType.MOUSE_MOVE,
                      button=MouseButton.LEFT, modifiers=frozenset())
    assert pane.forward_mouse(move) is True
    assert pane._sel[1] == (pane._offset + pane._view_h - 1, 7)   # x passed through, y clamped
    assert pane._edge_drag == 1
    assert pane.control._down_abs is not None        # still armed — only MOUSE_UP disarms


def test_forward_mouse_up_completes_copy_at_bottom_row_and_resets_state(monkeypatch):
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    captured = {}
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: captured.setdefault("text", text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(40)))
    pane._offset = 10

    down = MouseEvent(position=Point(1, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                 # Point is (x, y) → anchor abs (12, 1)

    # Delivered at the SAME viewport point the press used — a real in-pane
    # MOUSE_UP would skip the copy as a same-position click, but a FORWARDED
    # release only ever reaches here because the pointer already left the
    # pane, so it's a drag by definition; the click-vs-drag check must not
    # apply.
    bottom = pane._offset + pane._view_h - 1   # captured BEFORE the call — the
                                                # post-copy notify() re-follows
                                                # the tail and moves _offset on
    up = MouseEvent(position=Point(1, 2), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert pane.forward_mouse(up) is True

    assert captured["text"] == pane._selected_text((12, 1), (bottom, 1))
    assert pane.control._down is None
    assert pane.control._down_abs is None
    assert pane._sel is None
    assert pane._edge_drag == 0


def test_forward_mouse_clamp_top_extends_selection_to_top_row_and_arms_negative_edge_drag():
    # FIX6: clamp="top" (the tab bar, ABOVE the pane) must clamp y to the
    # pane's OWN top row (pane._offset) -- not the bottom row clamp="bottom"
    # (the default, used by the queue/todo panels/toolbar BELOW the pane)
    # uses -- and arm edge_drag=-1 (matching _SelectControl's own sign for a
    # real top-edge drag), so the ticker keeps scrolling UP + growing the
    # selection while the pointer sits parked on the tab bar.
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 20

    down = MouseEvent(position=Point(1, 3), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                 # arms the drag INSIDE the pane

    move = MouseEvent(position=Point(7, 0), event_type=MouseEventType.MOUSE_MOVE,
                      button=MouseButton.LEFT, modifiers=frozenset())
    assert pane.forward_mouse(move, clamp="top") is True
    assert pane._sel[1] == (pane._offset, 7)          # x passed through, y clamped to TOP row
    assert pane._edge_drag == -1                       # negative -- matches a real top-edge drag
    assert pane.control._down_abs is not None          # still armed — only MOUSE_UP disarms


def test_forward_mouse_clamp_top_up_completes_copy_at_top_row(monkeypatch):
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    captured = {}
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: captured.setdefault("text", text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(40)))
    pane._offset = 10

    down = MouseEvent(position=Point(1, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                  # Point is (x, y) → anchor abs (12, 1)

    top = pane._offset                                 # the pane's OWN top row
    up = MouseEvent(position=Point(1, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert pane.forward_mouse(up, clamp="top") is True

    assert captured["text"] == pane._selected_text((12, 1), (top, 1))
    assert pane.control._down is None
    assert pane.control._down_abs is None
    assert pane._sel is None
    assert pane._edge_drag == 0


def test_tab_bar_close_fragment_with_armed_pane_drag_completes_copy_not_close(monkeypatch):
    # FIX6, end-to-end wiring: a drag armed inside the output pane, released
    # on a tab's ✕ -- exactly the shape `tui._tab_fragments_live`'s
    # `forward=lambda ev: _pane().forward_mouse(ev, clamp="top")` produces.
    # The copy must complete (clipboard captured) and the close must NEVER
    # fire; every drag field resets, same as any other completed selection.
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from webbee.slots import SessionSlot, SlotManager
    from webbee.tabs import tab_fragments
    from webbee.tui import OutputPane

    captured = {}
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: captured.setdefault("text", text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(40)))
    pane._offset = 10

    down = MouseEvent(position=Point(1, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                  # arms the drag INSIDE the pane

    slots = SlotManager()
    slots.add(SessionSlot(kind="home", workspace=".", label="Home",
                          pane=OutputPane(width=80), sink=None, agent=None))
    slots.add(SessionSlot(kind="session", workspace=".", label="alpha",
                          pane=pane, sink=_idle_sink(), agent=None))
    slots.active_idx = 1
    switch_hits, close_hits = [], []
    forward = lambda ev: pane.forward_mouse(ev, clamp="top")   # noqa: E731 -- the REAL production seam
    frags = tab_fragments(slots, on_switch=switch_hits.append, on_close=close_hits.append,
                          forward=forward)
    close_alpha = frags[4][2]                          # the tab's own ✕ GLYPH fragment's handler (pad, glyph, pad)

    top = pane._offset
    up = MouseEvent(position=Point(1, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert close_alpha(up) is None                      # consumed -- same contract as a real key binding

    assert captured["text"] == pane._selected_text((12, 1), (top, 1))   # the copy completed
    assert close_hits == []                              # NO close fired
    assert switch_hits == []
    # every drag field reset -- a stray SECOND click doesn't see a stale drag
    assert pane.control._down_abs is None
    assert pane._sel is None
    assert pane._edge_drag == 0


def test_tab_bar_close_fragment_with_no_armed_drag_still_closes_as_before():
    # Sanity companion: an UNARMED pane (no drag in progress) must leave the
    # ✕ click's normal close dispatch completely untouched.
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from webbee.slots import SessionSlot, SlotManager
    from webbee.tabs import tab_fragments
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    slots = SlotManager()
    slots.add(SessionSlot(kind="home", workspace=".", label="Home",
                          pane=OutputPane(width=80), sink=None, agent=None))
    slots.add(SessionSlot(kind="session", workspace=".", label="alpha",
                          pane=pane, sink=_idle_sink(), agent=None))
    slots.active_idx = 1
    close_hits = []
    forward = lambda ev: pane.forward_mouse(ev, clamp="top")   # noqa: E731
    frags = tab_fragments(slots, on_switch=lambda i: None, on_close=close_hits.append,
                          forward=forward)
    close_alpha = frags[4][2]
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert close_alpha(up) is None
    assert close_hits == [1]


def test_forward_mouse_ignores_other_event_types_while_armed():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._io.write("hello")
    down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)

    scroll = MouseEvent(position=Point(0, 0), event_type=MouseEventType.SCROLL_UP,
                        button=MouseButton.LEFT, modifiers=frozenset())
    assert pane.forward_mouse(scroll) is False
    assert pane.control._down_abs is not None         # untouched — still armed


# ── W2 final-review Fix 3a: a MOUSE_DOWN forwarded from a neighbor while a
# drag is still armed means the matching MOUSE_UP was lost past that neighbor
# (or further) — reset every stale drag field and let the neighbor's own
# click proceed untouched (no phantom copy, no swallowed pull/toggle). ─────

def test_forward_mouse_down_while_armed_resets_state_and_returns_false():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._io.write("hello")
    down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                 # arm a drag, never released — stale
    assert pane.control._down_abs is not None

    stray_down = MouseEvent(position=Point(3, 1), event_type=MouseEventType.MOUSE_DOWN,
                            button=MouseButton.LEFT, modifiers=frozenset())
    assert pane.forward_mouse(stray_down) is False    # NOT consumed — the neighbor's click proceeds
    assert pane.control._down is None
    assert pane.control._down_abs is None
    assert pane._sel is None
    assert pane._edge_drag == 0


def test_forward_mouse_down_while_stale_armed_lets_wrapped_pull_fire_clean(monkeypatch):
    # End-to-end (mirrors test_release_below_pane_completes_copy_and_
    # suppresses_queue_pull): a queue row's own mouse_handler wraps `forward`
    # exactly like tui wires it. A stray MOUSE_DOWN landing on that row while
    # the pane's drag is stale-armed must not fire a phantom copy — and the
    # row's OWN MOUSE_UP (the click completing normally) must still pull,
    # with the clipboard never touched by this whole sequence.
    from collections import deque
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.queue_panel import queue_fragments
    from webbee.tui import OutputPane

    calls = []
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: calls.append(text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(40)))
    pane._offset = 10
    down = MouseEvent(position=Point(1, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                  # arm a drag on the pane, stale (no MOUSE_UP)

    pulls = []
    frags = queue_fragments(deque(["a", "b"]), pull=pulls.append, width=80,
                            forward=pane.forward_mouse)
    row_handler = [f[2] for f in frags if len(f) == 3][0]

    stray_down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                            button=MouseButton.LEFT, modifiers=frozenset())
    assert row_handler(stray_down) is NotImplemented   # not consumed — the click's press proceeds
    assert pane.control._down_abs is None              # the stale drag is fully cleared
    assert pane._sel is None

    stray_up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                          button=MouseButton.LEFT, modifiers=frozenset())
    assert row_handler(stray_up) is None               # the click's OWN release
    assert pulls == [0]                                # fires the pull normally
    assert calls == []                                 # clipboard UNTOUCHED — no phantom copy


# ── W2 final-review Fix 3b: edge-drag runaway guards — the user's wheel wins
# over an armed auto-scroll, and a pointer genuinely parked at the edge for
# ~10s (40 ticks) stops the auto-scroll on its own (selection stays armed;
# a MOUSE_DOWN/forward hygiene reset is what actually disarms the drag). ───

def test_scroll_wheel_during_armed_edge_drag_disarms_ticking_but_keeps_selection():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 0

    down = MouseEvent(position=Point(0, 5), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)
    move = MouseEvent(position=Point(3, 9), event_type=MouseEventType.MOUSE_MOVE,   # bottom edge
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)
    assert pane._edge_drag == 1
    offset_before_wheel = pane._offset

    wheel = MouseEvent(position=Point(3, 5), event_type=MouseEventType.SCROLL_DOWN,
                       button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(wheel)

    assert pane._edge_drag == 0                        # the wheel wins — auto-scroll disarmed
    assert pane._offset == offset_before_wheel + 3      # the user's own wheel scroll still happened
    assert pane._sel is not None                        # the armed selection itself STAYS
    assert pane.control._down_abs is not None            # the drag itself is still live


def test_edge_tick_stops_after_40_ticks_without_fresh_motion():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(10000)))
    pane._offset = 100

    down = MouseEvent(position=Point(0, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)
    move = MouseEvent(position=Point(0, 4), event_type=MouseEventType.MOUSE_MOVE,   # bottom edge
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)
    assert pane._edge_drag == 1

    for _ in range(40):
        pane.edge_tick()
    assert pane._edge_drag == 1                          # still ticking through the 40th

    pane.edge_tick()                                     # the 41st tick without fresh motion
    assert pane._edge_drag == 0                           # stops the auto-scroll
    assert pane._sel is not None                          # selection stays armed
    assert pane.control._down_abs is not None


def test_edge_tick_counter_resets_on_fresh_drag_mouse_move():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(10000)))
    pane._offset = 100

    down = MouseEvent(position=Point(0, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)
    move = MouseEvent(position=Point(0, 4), event_type=MouseEventType.MOUSE_MOVE,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)

    for _ in range(39):
        pane.edge_tick()
    assert pane._edge_ticks == 39

    pane.control.mouse_handler(move)                      # a fresh drag MOUSE_MOVE resets the clock
    assert pane._edge_ticks == 0

    for _ in range(40):
        pane.edge_tick()
    assert pane._edge_drag == 1                            # the reset bought another 40 ticks


def test_mouse_down_while_already_armed_resets_stale_edge_drag():
    # W1-recon stuck-highlight hygiene: a release lost past a neighbor window
    # (the exact bug this task fixes for queue/todo/toolbar, but SOME window
    # is always uncovered — e.g. the input box) must not leave a stale
    # `_edge_drag` armed forever; the NEXT press cleans it up.
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(100)))
    pane._offset = 0

    down1 = MouseEvent(position=Point(0, 5), event_type=MouseEventType.MOUSE_DOWN,
                       button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down1)
    move = MouseEvent(position=Point(3, 9), event_type=MouseEventType.MOUSE_MOVE,   # bottom edge
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(move)
    assert pane._edge_drag == 1
    assert pane.control._down_abs is not None          # no MOUSE_UP ever arrived — stuck

    # A fresh MOUSE_DOWN, without an intervening MOUSE_UP. offset is now 3
    # (the edge-scroll from `move`, above) — Point is (x, y), so this press
    # at viewport (x=2, y=4) anchors abs (7, 2).
    down2 = MouseEvent(position=Point(2, 4), event_type=MouseEventType.MOUSE_DOWN,
                       button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down2)
    assert pane._edge_drag == 0                        # stale flag cleared
    assert pane.control._down_abs == (7, 2)             # re-armed at the NEW press
    assert pane._sel == ((7, 2), (7, 2))


def test_forwarding_wrapper_suppresses_wrapped_handler_when_drag_armed(monkeypatch):
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane, _forwarding

    monkeypatch.setattr(clipboard, "copy_to_clipboard", lambda text: "✓ copied")

    pane = OutputPane(width=80)
    pane._io.write("abc")
    down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)

    calls = []
    wrapped = _forwarding(lambda ev: calls.append(ev) or "handler-ran", pane)
    up = MouseEvent(position=Point(4, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert wrapped(up) is None            # consumed by the pane, not the wrapped handler
    assert calls == []
    assert pane.control._down_abs is None  # the pane really did complete/reset the drag


def test_forwarding_wrapper_falls_through_when_no_drag_armed():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane, _forwarding

    pane = OutputPane(width=80)
    calls = []
    wrapped = _forwarding(lambda ev: calls.append(ev) or "handler-ran", pane)
    up = MouseEvent(position=Point(4, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert wrapped(up) == "handler-ran"
    assert calls == [up]


def test_forwarding_wrapper_returns_notimplemented_for_a_none_handler(monkeypatch):
    # The toolbar has no mouse handling of its own — _forwarding(None, pane)
    # is wrapped purely to give the pane first refusal.
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.tui import OutputPane, _forwarding

    monkeypatch.setattr(clipboard, "copy_to_clipboard", lambda text: "✓ copied")

    pane = OutputPane(width=80)
    pane._io.write("abc")
    wrapped = _forwarding(None, pane)
    up = MouseEvent(position=Point(4, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert wrapped(up) is NotImplemented                # no drag armed, no handler to fall to

    down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)
    assert wrapped(up) is None                          # armed now — consumed


def test_release_below_pane_completes_copy_and_suppresses_queue_pull(monkeypatch):
    # End-to-end: the ACTUAL seam tui wires — queue_fragments(forward=pane.
    # forward_mouse) — delivers a forwarded MOUSE_UP to a queue row exactly
    # like a real click would, and the copy completes instead of the pull.
    from collections import deque
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.queue_panel import queue_fragments
    from webbee.tui import OutputPane

    captured = {}
    monkeypatch.setattr(clipboard, "copy_to_clipboard",
                        lambda text: captured.setdefault("text", text) or "✓ copied")

    pane = OutputPane(width=80)
    pane._view_h = 5
    pane._io.write("\n".join(f"line{i}" for i in range(40)))
    pane._offset = 10
    down = MouseEvent(position=Point(1, 2), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)                  # arm a drag on the pane

    pulls = []
    frags = queue_fragments(deque(["a", "b"]), pull=pulls.append, width=80,
                            forward=pane.forward_mouse)
    row_handler = [f[2] for f in frags if len(f) == 3][0]

    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,   # the row's own y/x
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert row_handler(up) is None
    assert pulls == []                                # NOT pulled — the pane claimed the release
    assert "text" in captured                         # the copy fired
    assert pane.control._down_abs is None


def test_forward_noop_when_no_drag_armed_queue_pull_still_works():
    # Mirror of the above with no drag armed: the wrapped pull fires exactly
    # as before the forwarding seam existed — the wrapper is transparent.
    from collections import deque
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.queue_panel import queue_fragments
    from webbee.tui import OutputPane

    pane = OutputPane(width=80)
    pulls = []
    frags = queue_fragments(deque(["a", "b"]), pull=pulls.append, width=80,
                            forward=pane.forward_mouse)
    row_handler = [f[2] for f in frags if len(f) == 3][0]
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert row_handler(up) is None
    assert pulls == [0]


def test_queue_header_toggle_forward_param_suppresses_toggle_when_armed(monkeypatch):
    from collections import deque
    import webbee.clipboard as clipboard
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from webbee.queue_panel import queue_fragments
    from webbee.tui import OutputPane

    monkeypatch.setattr(clipboard, "copy_to_clipboard", lambda text: "✓ copied")

    pane = OutputPane(width=80)
    pane._io.write("abc")
    down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    pane.control.mouse_handler(down)

    hits = []
    frags = queue_fragments(deque(["a"]), toggle=lambda: hits.append(1),
                            forward=pane.forward_mouse)
    header_handler = frags[0][2]
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert header_handler(up) is None
    assert hits == []                                 # suppressed — the pane claimed it
    assert pane.control._down_abs is None


# ── virtualization: render only the visible slice, follow the tail ────────────

def test_pane_follows_tail_on_notify():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(50)))   # 50 lines
    pane.notify()
    assert pane._offset == len(pane._all_lines()) - 10          # pinned to the bottom


def test_pane_scroll_up_pauses_follow_then_rearms():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(50)))
    pane.notify()                      # offset=40, following
    pane.scroll(-5)
    assert pane._offset == 35 and pane._follow is False
    pane.scroll(100)                   # clamp back to bottom
    assert pane._offset == 40 and pane._follow is True


def test_selected_text_ignores_offset_now_absolute():
    # _selected_text no longer re-adds `_offset` — its start/end are already
    # ABSOLUTE lines, so a scrolled viewport is irrelevant to this call (the
    # mouse handler is the one place `_offset` gets applied, exactly once,
    # at press/move/release time — see test_selection_survives_scroll_*).
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane._io.write("aaa\nbbb\nccc\nddd\n")
    pane._offset = 2                   # scrolled — must NOT affect the result below
    assert pane._selected_text((2, 0), (2, 2)) == "ccc"


def test_output_pane_no_full_reread_on_unchanged_redraw():
    # Perf regression (long-session lag): _all_lines re-read the ENTIRE buffer
    # (getvalue O(n) + full string compare O(n)) on EVERY redraw. Every keystroke
    # / ticker tick / scroll in a big session cost O(session). With NO new output,
    # a redraw must not re-read the whole buffer; WITH new output (Task 14), the
    # cache is extended via a positional DELTA read (O(new output)) instead of a
    # full getvalue() re-split — so getvalue() is never called at all here.
    import io

    from webbee.output_pane import OutputPane

    class CountingIO(io.StringIO):
        def __init__(self):
            super().__init__()
            self.gv = 0

        def getvalue(self):
            self.gv += 1
            return super().getvalue()

    pane = OutputPane(width=80)
    cio = CountingIO()
    pane._io = cio
    pane.console.file = cio
    pane.console.print("some output line")
    pane._all_lines()                      # warm the cache
    base = cio.gv
    pane._all_lines(); pane._all_lines(); pane._all_lines()   # no new writes since
    assert cio.gv == base, "re-read the whole buffer on unchanged redraws (O(session)/frame)"
    pane.console.print("a new line")       # content changed -> incremental delta, not getvalue()
    assert any("a new line" in ln for ln in pane._all_lines())
    assert cio.gv == base, "a print must extend the cache via delta read, not a full getvalue() re-split"


# ── Task 14: incremental line caches + hysteresis trim ──────────────────────
# Even with the getvalue()-memoized-by-position cache above, a changed redraw
# still re-split the WHOLE buffer from scratch (getvalue() + str.split("\n")
# over the entire session) — O(session) per print, quadratic over a long busy
# stream. The cache must extend the cached list IN PLACE from only the delta
# written since the last split.

def test_all_lines_appends_delta_in_place():
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    p.console.print("one")
    first = p._all_lines()
    p.console.print("two")
    second = p._all_lines()
    assert second is first                      # same list object, extended
    assert any("two" in ln for ln in second[-3:])


def test_trim_hysteresis_and_offset_preserved():
    # W2 final-review Fix 1: the buffer's front `_ring_base_lines` lines are
    # pre-ring (deque-evicted equivalent) — freely droppable. Simulate a
    # realistic post-eviction session (21000 lines total, the newest 4000
    # still ring-backed) directly, rather than 21000 real console.print()
    # calls, for test speed.
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(21000):
        p._io.write(f"line{i}\n")
    p._lines_cache = (None, [""])               # force re-split
    p._record_lines = [1] * 4000                # newest 4000 lines are ring-backed
    p._records.extend([(("x",), {})] * 4000)    # matching placeholders (popleft-count only)
    p._ring_base_lines = 21000 - 4000           # everything older is pre-ring base
    p._offset = 20500                           # reader scrolled up
    ring_invariant(p)
    p._trim()
    lines = p._all_lines()
    assert len(lines) <= 15001                  # cut to ~15000 (+trailing)
    ring_invariant(p)
    assert len(p._records) == 4000              # the base absorbed the WHOLE cut — ring untouched
    dropped = 21001 - len(lines)
    assert p._offset == max(0, 20500 - dropped) # view anchored to same content


def test_trim_below_threshold_leaves_buffer_untouched():
    # Hysteresis negative case: below max_lines (20000), _trim must be a
    # complete no-op -- no rewrite of the underlying StringIO at all.
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(18000):
        p._io.write(f"line{i}\n")
    p._lines_cache = (None, [""])               # force re-split
    before = p._io.getvalue()
    p._trim()
    assert p._io.getvalue() == before


def test_trim_keep_floor_not_over_aggressive():
    # A trim that DOES fire must cut down to ~keep (15000), not slash way
    # below it -- pins the keep floor so a future change to the hysteresis
    # constants can't silently turn this into an over-aggressive cut.
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(21000):
        p._io.write(f"line{i}\n")
    p._lines_cache = (None, [""])
    p._record_lines = [1] * 4000
    p._records.extend([(("x",), {})] * 4000)
    p._ring_base_lines = 21000 - 4000
    p._trim()
    ring_invariant(p)
    lines = p._all_lines()
    assert len(lines) >= 14000


# ── W2 final-review Fix 1: _trim never splits a ring record — the cut
# consumes the base first, then only WHOLE leading records, moving the
# actual cut UP to the nearest record boundary. ────────────────────────────

def test_trim_consumes_base_before_touching_ring_records():
    # dropped < base -> the base alone absorbs the WHOLE cut; the ring is
    # never even inspected, let alone split.
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(500):
        p._io.write(f"line{i}\n")
    p._record_lines = [1] * 200
    p._records.extend([(("x",), {})] * 200)
    p._ring_base_lines = 500 - 200                 # base=300, ring=200 (total 501 w/ trailing)
    ring_invariant(p)

    p._trim(max_lines=400, keep=350)                # dropped = 501-350 = 151 < base(300)
    ring_invariant(p)

    assert len(p._records) == 200                   # ring completely untouched
    assert p._record_lines == [1] * 200
    assert p._ring_base_lines == 300 - 151           # the base alone absorbed the cut


def test_trim_never_splits_a_record_moves_cut_up_to_the_boundary():
    # A real (eviction-backed) session: 4500 single-line console.print()s,
    # ring-capped at 4000 -> base=500 lines, 4000 records. A trim whose
    # naive target falls MID-RECORD must move UP to the record boundary
    # instead of splitting one — the exact bug this fix closes (a
    # post-trim reflow could otherwise resurrect a half-trimmed record).
    from webbee.output_pane import OutputPane

    p = OutputPane(width=60)
    for i in range(4500):
        p.console.print(str(i))                     # each print is exactly one line
    assert p._ring_base_lines == 500
    assert len(p._records) == 4000
    ring_invariant(p)

    p._trim(max_lines=4000, keep=3000)               # dropped=1501; base(500) covers 500 of it,
                                                       # leaving 1001 lines to cut from the ring —
                                                       # exactly 1001 single-line records, no partial
    ring_invariant(p)

    assert p._ring_base_lines == 0                    # base fully consumed
    assert len(p._records) == 4000 - 1001              # exactly 1001 WHOLE records dropped
    assert len(p._all_lines()) == 3000                  # aligned exactly (each record = 1 line)


def test_trim_never_empties_the_ring_while_non_ring_lines_remain():
    from webbee.output_pane import OutputPane

    p = OutputPane(width=60)
    for i in range(4500):
        p.console.print(str(i))
    assert p._ring_base_lines == 500

    p._trim(max_lines=4000, keep=4001)                # dropped=500, exactly base(500) -> no records
    ring_invariant(p)

    assert len(p._records) == 4000                     # ring untouched — never even approached


# ── W2 final-review Fix 5: a trim shifts an ARMED drag's anchors so the
# highlight and eventual copy stay on the same CONTENT across the cut. ─────

def test_trim_shifts_armed_drag_anchors_by_actual_dropped():
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(500):
        p.console.print(str(i))          # 500 single-line records, base stays 0 (< 4000 ring cap)
    assert p._ring_base_lines == 0
    p.control._down_abs = (300, 5)
    p._sel = ((300, 5), (350, 7))

    p._trim(max_lines=400, keep=300)      # dropped=201, base=0 -> actual_dropped=201 (record-aligned)
    ring_invariant(p)

    assert p.control._down_abs == (99, 5)             # 300 - 201
    assert p._sel == ((99, 5), (149, 7))              # both endpoints shifted identically


def test_trim_clamps_shifted_drag_anchor_row_at_zero():
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(500):
        p.console.print(str(i))
    p.control._down_abs = (10, 2)          # anchor near the very top
    p._sel = ((10, 2), (20, 3))

    p._trim(max_lines=400, keep=300)       # actual_dropped=201 > 10 and > 20 -> both would go negative
    ring_invariant(p)

    assert p.control._down_abs == (0, 2)   # clamped, never negative
    assert p._sel == ((0, 2), (0, 3))


def test_trim_leaves_unarmed_selection_state_untouched():
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(500):
        p.console.print(str(i))
    assert p.control._down_abs is None
    assert p._sel is None

    p._trim(max_lines=400, keep=300)
    ring_invariant(p)

    assert p.control._down_abs is None
    assert p._sel is None


# ── W2 Task 2: RecordingConsole — bounded ring of every printed renderable ──
# The old pane kept only baked ANSI, which can never re-wrap on a width
# change. Every console.print() now also appends (objects, kw) to a bounded
# ring so a future terminal-width change can REPLAY the transcript at the
# new width (Task 3). The ring is bounded (_MAX_RECORDS=4000) — the honest
# trade the spec accepted: a session past that only replays the newest tail.

def test_recording_console_captures_renderables():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=60)
    p.console.print("hello")
    from rich.text import Text
    p.console.print(Text("styled"), style="bold")
    assert len(p._records) == 2
    assert p._records[0][0] == ("hello",)
    assert p._records[1][1].get("style") == "bold"


def test_recording_console_clear_resets_ring_and_buffer():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=60)
    p.console.print("x")
    p.console.clear()
    assert len(p._records) == 0
    assert p._all_lines() == [""]


def test_record_ring_bounded():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=60)
    for i in range(4100):
        p.console.print(str(i))
    assert len(p._records) == 4000          # oldest fell off — replay covers the tail


# ── W2 Task 3: reflow — width change replays the ring, offset anchored by
# the RECORD under the top visible line (a line index is meaningless across
# a re-wrap; the record that produced it is the only stable anchor). ────────

def test_reflow_rewraps_all_content():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=100)
    p.console.print("word " * 40)                  # one long line at width 100
    wide_lines = len(p._all_lines())
    p.reflow(40)
    narrow_lines = len(p._all_lines())
    assert p.console.width == 40
    assert narrow_lines > wide_lines               # re-wrapped, not clipped
    assert all(len(strip_ansi(ln)) <= 40 for ln in p._all_lines())


def test_reflow_noop_on_same_width():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=80)
    p.console.print("x")
    buf_before = p._io.getvalue()
    p.reflow(80)
    assert p._io.getvalue() == buf_before


def test_reflow_anchors_scrolled_up_offset_by_record():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=100)
    for i in range(200):
        p.console.print(f"record-{i} " + "pad " * 30)
    p._view_h = 10
    p.scroll(-150)                                  # scrolled well up
    top_record = p._record_at_line(p._offset)
    p.reflow(50)
    assert p._record_at_line(p._offset) == top_record   # same CONTENT on top


def test_reflow_preserves_tail_follow():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=100)
    for i in range(50):
        p.console.print("x " * 40)
    assert p._follow
    p.reflow(60)
    lines = p._all_lines()
    assert p._offset == max(0, len(lines) - max(1, p._view_h))


def test_reflow_clears_active_selection():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=80)
    p.console.print("abc")
    p._sel = ((0, 0), (0, 2))
    p.reflow(60)
    assert p._sel is None


def test_reflow_aborts_an_in_progress_drag():
    # A resize mid mouse-drag must abort the drag honestly, not leave a
    # stale mouse-down anchor pointing at pre-rewrap coordinates.
    from webbee.output_pane import OutputPane
    p = OutputPane(width=80)
    p.console.print("abc")
    p.control._down = (0, 0)          # simulate an in-progress MOUSE_DOWN
    p.reflow(60)
    assert p.control._down is None


def test_reflow_noop_below_minimum_width():
    # A pathologically narrow resize (e.g. a terminal briefly reporting 0-9
    # cols mid-drag) must not corrupt state — clamp to a no-op.
    from webbee.output_pane import OutputPane
    p = OutputPane(width=80)
    p.console.print("x")
    buf_before = p._io.getvalue()
    p.reflow(5)
    assert p.console.width == 80
    assert p._io.getvalue() == buf_before


def test_reflow_empty_pane_does_not_crash():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=80)
    p.reflow(40)
    assert p.console.width == 40
    assert p._offset == 0


def test_ring_eviction_keeps_record_lines_in_lockstep_with_records():
    # _record_lines must shrink in lockstep with the bounded _records deque —
    # otherwise, once a long session evicts old records, _record_at_line's
    # prefix sum drifts out of alignment with what's actually replayable.
    from webbee.output_pane import OutputPane
    p = OutputPane(width=60)
    for i in range(4100):
        p.console.print(str(i))
    assert len(p._records) == 4000
    assert len(p._record_lines) == len(p._records)


def test_reflow_does_not_duplicate_records_or_reenter_recording():
    # The replay must go through the base Console.print, never back through
    # the RecordingConsole override — else every reflow would double the ring.
    from webbee.output_pane import OutputPane
    p = OutputPane(width=100)
    for i in range(10):
        p.console.print(f"line-{i}")
    n_records = len(p._records)
    p.reflow(50)
    assert len(p._records) == n_records
    assert sum(p._record_lines) == len(p._all_lines()) - 1


# ── W2 final-review Fix 2: reflow preserves PRE-RING scrollback (deque
# eviction) at its OLD (already-baked) width instead of deleting it, while
# the ring itself still genuinely re-wraps. ─────────────────────────────────

def test_reflow_preserves_pre_ring_scrollback_at_old_width():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=100)
    for i in range(4050):                        # 50 evicted -> base grows past record 0
        p.console.print(f"record-{i}")
    assert p._ring_base_lines > 0
    base = p._ring_base_lines
    p._view_h = 10
    p._follow = False
    p._offset = base - 5                          # squarely inside the PRE-RING region
    pre_before = p._all_lines()[:base]

    p.reflow(60)
    ring_invariant(p)

    assert p._offset == base - 5                  # unchanged — pre-ring lines don't re-wrap
    assert p._all_lines()[:base] == pre_before     # same content, same (OLD) width
    assert p._ring_base_lines == base              # the COUNT never changes on reflow


def test_reflow_anchors_ring_region_by_record_with_nonzero_base():
    # Mirror of test_reflow_anchors_scrolled_up_offset_by_record, but with a
    # nonzero base (post-eviction) — proves the base-adjusted _record_at_line
    # math, not just the base==0 case.
    from webbee.output_pane import OutputPane
    p = OutputPane(width=100)
    for i in range(4200):
        p.console.print(f"record-{i} " + "pad " * 30)
    assert p._ring_base_lines > 0
    p._view_h = 10
    p._follow = False
    p._offset = p._ring_base_lines + 20            # inside the RING region, not pre-ring
    top_record = p._record_at_line(p._offset)

    p.reflow(50)
    ring_invariant(p)

    assert p._record_at_line(p._offset) == top_record   # same CONTENT on top, base-adjusted
    assert p._offset >= p._ring_base_lines               # anchored back into the ring, not pre-ring


def test_reflow_preserves_tail_follow_with_nonzero_base():
    from webbee.output_pane import OutputPane
    p = OutputPane(width=100)
    for i in range(4100):
        p.console.print("x " * 40)
    assert p._follow
    assert p._ring_base_lines > 0

    p.reflow(60)
    ring_invariant(p)

    lines = p._all_lines()
    assert p._offset == max(0, len(lines) - max(1, p._view_h))


# ── W2 Task 4: _width_watch — the ticker's per-tick bridge from PT's
# SIGWINCH repaint to the Rich-side reflow ──────────────────────────────────

def test_ticker_width_watch_triggers_reflow(monkeypatch):
    """Drive the extracted _width_watch(pane, app) helper directly: app
    reports 72 cols while pane.console.width is 100 ⇒ pane.reflow(72) called
    (record with a stub); same width ⇒ no call."""
    from webbee.tui import _width_watch

    class _Pane:
        def __init__(self, width):
            self.console = type("Console", (), {"width": width})()
            self.calls = []

        def reflow(self, cols):
            self.calls.append(cols)

    monkeypatch.setattr("webbee.sizing.get_size", lambda app: (72, 24))
    pane = _Pane(100)
    _width_watch(pane, object())
    assert pane.calls == [72]

    same = _Pane(72)
    _width_watch(same, object())
    assert same.calls == []


def test_ticker_width_watch_swallows_reflow_error(monkeypatch):
    """A reflow crash must never kill the ticker — it's the dock's only
    animation loop (spinner + queued-line drains all ride on it)."""
    from webbee.tui import _width_watch

    class _BrokenPane:
        def __init__(self, width):
            self.console = type("Console", (), {"width": width})()

        def reflow(self, cols):
            raise RuntimeError("boom")

    monkeypatch.setattr("webbee.sizing.get_size", lambda app: (72, 24))
    _width_watch(_BrokenPane(100), object())   # must not raise


# ── W2 final-review Fix 7: _tick_once — the ticker body, extracted so the
# wiring itself (not just its pieces) is directly unit-testable. ───────────

def test_tick_once_fires_width_watch_edge_tick_and_invalidate(monkeypatch):
    from webbee.tui import _tick_once

    calls = {"reflow": [], "edge_tick": 0, "invalidate": 0}

    class _Pane:
        def __init__(self):
            self.console = type("Console", (), {"width": 100})()

        def reflow(self, cols):
            calls["reflow"].append(cols)

        def edge_tick(self):
            calls["edge_tick"] += 1

        def flash(self):
            return ""

    class _App:
        def invalidate(self):
            calls["invalidate"] += 1

    monkeypatch.setattr("webbee.sizing.get_size", lambda app: (72, 24))
    _tick_once(mk_slots(pane=_Pane()), _App(), lambda: True)

    assert calls["reflow"] == [72]     # _width_watch fired (resize bridge)
    assert calls["edge_tick"] == 1     # pane.edge_tick() fired
    assert calls["invalidate"] == 1    # is_busy() True -> app.invalidate() fired


def test_tick_once_swallows_edge_tick_error(monkeypatch):
    from webbee.tui import _tick_once

    class _Pane:
        def __init__(self):
            self.console = type("Console", (), {"width": 72})()

        def edge_tick(self):
            raise RuntimeError("boom")

        def flash(self):
            return ""

    class _App:
        def invalidate(self):
            pass

    monkeypatch.setattr("webbee.sizing.get_size", lambda app: (72, 24))
    _tick_once(mk_slots(pane=_Pane()), _App(), lambda: False)   # must not raise


def test_tick_once_drives_the_active_slots_pane_after_a_switch(monkeypatch):
    # W4a Task 3: _tick_once now takes the SlotManager, not a bound pane --
    # it must re-resolve slots.active().pane EVERY call, so a tab switch
    # immediately redirects the ticker's edge_tick/width_watch at the
    # newly-visible slot's own pane, never a stale reference to whichever
    # slot was active when the ticker started.
    from webbee.slots import SessionSlot, SlotManager
    from webbee.tui import _tick_once

    class _Pane:
        def __init__(self, width):
            self.console = type("Console", (), {"width": width})()
            self.reflow_calls = []
            self.edge_ticks = 0

        def reflow(self, cols):
            self.reflow_calls.append(cols)

        def edge_tick(self):
            self.edge_ticks += 1

        def flash(self):
            return ""

    class _App:
        def invalidate(self):
            pass

    pane_a, pane_b = _Pane(100), _Pane(100)
    slots = mk_slots(pane=pane_a)
    slots.add(SessionSlot(kind="session", workspace=".", label="b",
                          pane=pane_b, sink=None, agent=None))

    monkeypatch.setattr("webbee.sizing.get_size", lambda app: (72, 24))
    app = _App()
    _tick_once(slots, app, lambda: False)
    assert pane_a.reflow_calls == [72] and pane_a.edge_ticks == 1
    assert pane_b.reflow_calls == [] and pane_b.edge_ticks == 0   # B untouched while A active

    slots.active_idx = 1                                          # switch to B
    _tick_once(slots, app, lambda: False)
    assert pane_b.reflow_calls == [72] and pane_b.edge_ticks == 1  # NOW B is driven
    assert pane_a.reflow_calls == [72] and pane_a.edge_ticks == 1  # A untouched further


# ── W2 Task 5: input_rows — the pure estimator behind _input_height ─────────
# Extracted like repl._gate_busy: module-level, dependency-injected (cols/cap
# passed in), so a test drives the exact wrap math without a live app or
# terminal. The closure (_input_height) feeds it sizing.get_size()'s rows via
# sizing.input_height_cap — proportional, not the old hardcoded 10.

def test_input_rows_pure_wrap_math():
    from webbee.tui import input_rows
    assert input_rows("", 40, 10) == 1                 # empty draft is always 1 row
    assert input_rows("short", 40, 10) == 1
    assert input_rows("x" * 90, 40, 10) == 3            # ceil(90/40) == 3
    assert input_rows("a\nb\nc", 40, 10) == 3           # one row per line, no wrap needed


def test_input_rows_uses_the_injected_cap_not_a_fixed_ten():
    """rows=60 (tall terminal) -> cap 10 (ceiling); rows=24 -> cap 7 — the
    SAME pure estimator, only the injected cap changes."""
    from webbee.sizing import input_height_cap
    from webbee.tui import input_rows
    long_draft = "\n".join(["x" * 200] * 20)   # far more wrapped rows than any cap allows
    assert input_height_cap(60) == 10 and input_height_cap(24) == 7
    assert input_rows(long_draft, 40, input_height_cap(60)) == 10
    assert input_rows(long_draft, 40, input_height_cap(24)) == 7


def test_input_rows_floors_a_tiny_or_zero_width():
    from webbee.tui import input_rows
    assert input_rows("x" * 30, 0, 10) == 3             # cols floored at 10 -> ceil(30/10)


# ── P5g: Esc/Ctrl-C stop the SERVER turn, not just the local task ────────────
# Previously Ctrl-C only cancelled the local asyncio task while the cloud
# brain kept running server-side; Esc did nothing while busy. Both key
# bindings are wired through `_escape_action`/`_interrupt_action` (pure,
# dependency-injected — same testing philosophy as next_mode/build_toolbar)
# so this is exercised without spinning up a real prompt_toolkit Application.

class _FakeApp:
    def __init__(self):
        self.background_tasks = []
        self.invalidated = False

    def create_background_task(self, coro):
        self.background_tasks.append(coro)

    def invalidate(self):
        self.invalidated = True


class _FakeEvent:
    def __init__(self):
        self.app = _FakeApp()


def _run_and_drain(coro):
    """Actually execute a coroutine handed to create_background_task so it
    doesn't leak an 'never awaited' warning, and to prove the spy fired."""
    asyncio.run(coro)


class _FakeTask:
    def __init__(self): self.cancelled = []
    def done(self): return False
    def cancel(self): self.cancelled.append(1)


def test_escape_while_busy_cancels_turn_and_schedules_server_stop():
    # Esc now REALLY stops a running turn: cancel the local turn task (what tears
    # it down) AND ask the server to stop — matching the "Esc/Ctrl-C" hint.
    from webbee.tui import _escape_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    sel = {"i": 2}
    task = _FakeTask()
    turn = {"task": task}
    _escape_action(sel, turn, lambda: True, stop_turn, event)

    assert task.cancelled == [1]                  # the local turn is actually cancelled
    assert len(event.app.background_tasks) == 1
    _run_and_drain(event.app.background_tasks[0])
    assert stop_calls == [1]                       # AND the server is asked to stop
    # Busy-path stops the turn — it does NOT also clear the step-selection.
    assert sel["i"] == 2
    assert event.app.invalidated is False


def test_escape_while_idle_clears_step_selection_unchanged():
    from webbee.tui import _escape_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    sel = {"i": 2}
    turn = {"task": None}
    _escape_action(sel, turn, lambda: False, stop_turn, event)

    assert sel["i"] is None
    assert event.app.invalidated is True
    assert event.app.background_tasks == []
    assert stop_calls == []


def test_escape_while_busy_with_no_stop_turn_still_cancels_locally():
    # stop_turn=None (e.g. no repl wiring) must never crash Esc — it still cancels
    # the local turn task, so Esc reliably stops even without the server hook.
    from webbee.tui import _escape_action

    event = _FakeEvent()
    sel = {"i": 1}
    task = _FakeTask()
    turn = {"task": task}
    _escape_action(sel, turn, lambda: True, None, event)

    assert task.cancelled == [1]                  # local teardown happens regardless
    assert event.app.background_tasks == []        # no stop_turn -> nothing scheduled
    assert sel["i"] == 1                            # busy-path leaves the selection


def test_interrupt_while_busy_cancels_local_task_and_schedules_server_stop():
    from webbee.tui import _interrupt_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    cancelled = []

    class _FakeTask:
        def done(self):
            return False

        def cancel(self):
            cancelled.append(1)

    event = _FakeEvent()
    turn = {"task": _FakeTask()}
    _interrupt_action(turn, lambda: True, stop_turn, event)

    assert cancelled == [1]                      # local teardown still happens
    assert len(event.app.background_tasks) == 1  # AND the server gets asked to stop
    _run_and_drain(event.app.background_tasks[0])
    assert stop_calls == [1]


def test_interrupt_with_no_running_task_is_a_noop():
    from webbee.tui import _interrupt_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    turn = {"task": None}
    _interrupt_action(turn, lambda: True, stop_turn, event)

    assert event.app.background_tasks == []
    assert stop_calls == []


# ---- mouse-report flood hardening (0.3.3) ----------------------------------
# Linux (occasionally macOS) terminals under prompt_toolkit's default
# mouse_support=True run ANY-EVENT tracking (?1003): every bare mouse move
# fires an SGR report. Under that flood the vt100 parser splits sequences at
# read-chunk boundaries — the ESC arrives as a phantom Escape KEY (which used
# to stop the running turn!) and the tail ("35;6;42M…") lands in the input
# buffer as literal text.

_GARBAGE = "roo35;6;42M<35;35;46M5;49;46M3M35;71;37M29M5;94;19M5;101;14M4M"


def test_scrub_mouse_residue_removes_leaked_reports():
    from webbee.tui import scrub_mouse_residue
    out = scrub_mouse_residue(_GARBAGE)
    assert "42M" not in out and "<35" not in out
    assert out.startswith("roo")            # non-residue text survives


def test_scrub_mouse_residue_keeps_normal_text():
    from webbee.tui import scrub_mouse_residue
    for s in ("fix the tests", "a;b;c", "35;6", "надо дочинить endpoint'ы", ""):
        assert scrub_mouse_residue(s) == s


class _FakeOutput:
    def __init__(self):
        self.raw = []

    def write_raw(self, s):
        self.raw.append(s)


def test_configure_mouse_modes_button_event_not_any_event():
    from webbee.tui import configure_mouse_modes
    out = _FakeOutput()
    configure_mouse_modes(out)
    out.enable_mouse_support()
    joined = "".join(out.raw)
    assert "\x1b[?1002h" in joined           # drag/wheel/click tracking
    assert "\x1b[?1003h" not in joined       # NEVER any-event (bare-move flood)
    out.raw.clear()
    out.disable_mouse_support()
    joined = "".join(out.raw)
    assert "\x1b[?1002l" in joined and "\x1b[?1003l" in joined  # belt & braces


def test_configure_mouse_modes_skips_outputs_without_write_raw():
    from webbee.tui import configure_mouse_modes
    class _Plain:
        pass
    p = _Plain()
    configure_mouse_modes(p)                 # must not raise, must not add attrs
    assert not hasattr(p, "enable_mouse_support")


# ---- focus-report hardening (0.3.25) ---------------------------------------
# A tmux pane switch / OS window-focus change can leak DEC focus-in/out
# reports ("\x1b[I" / "\x1b[O") into stdin, same split-sequence hazard as the
# mouse residue above -- configure_mouse_modes now explicitly disables ?1004
# in BOTH paths, and the scrubber drops any leaked report that slips through.

def test_configure_mouse_modes_disables_focus_reporting_on_enable():
    from webbee.tui import configure_mouse_modes
    out = _FakeOutput()
    configure_mouse_modes(out)
    out.enable_mouse_support()
    assert "\x1b[?1004l" in out.raw


def test_configure_mouse_modes_disables_focus_reporting_on_disable():
    from webbee.tui import configure_mouse_modes
    out = _FakeOutput()
    configure_mouse_modes(out)
    out.disable_mouse_support()
    assert "\x1b[?1004l" in out.raw


def test_scrub_mouse_residue_removes_stray_focus_reports():
    from webbee.tui import scrub_mouse_residue
    out = scrub_mouse_residue("fix\x1b[Ithe\x1b[Otests")
    assert out == "fixthetests"


def test_scrub_mouse_residue_focus_report_requires_esc_prefix():
    # A bare "[I"/"[O" with no leading ESC is ordinary text (e.g. a citation
    # marker) -- never eaten, only the genuine ESC-prefixed report is.
    from webbee.tui import scrub_mouse_residue
    text = "see [I] and [O] in the docs"
    assert scrub_mouse_residue(text) == text


def test_scrub_mouse_residue_removes_both_mouse_and_focus_garbage_together():
    from webbee.tui import scrub_mouse_residue
    out = scrub_mouse_residue("roo35;6;42M\x1b[Itail\x1b[O")
    assert "42M" not in out and "\x1b[I" not in out and "\x1b[O" not in out
    assert out.startswith("roo")


class _FakeBuf:
    def __init__(self, text=""):
        self.text = text


def test_phantom_escape_with_residue_cleans_buffer_not_the_turn():
    # A mouse-report flood in the input buffer means this Escape is almost
    # certainly a SPLIT SEQUENCE, not the user — never kill the turn on it.
    from webbee.tui import _escape_action

    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    buf = _FakeBuf("draft " + _GARBAGE)
    _escape_action({"i": None}, turn, lambda: True, None, event, buf=buf)
    assert task.cancelled == []              # the turn SURVIVES
    assert "42M" not in buf.text             # the residue is cleaned
    assert buf.text.startswith("draft roo")


def test_real_escape_with_clean_buffer_still_stops_the_turn():
    from webbee.tui import _escape_action

    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    _escape_action({"i": None}, turn, lambda: True, None, event, buf=_FakeBuf("draft reply"))
    assert task.cancelled == [1]             # normal Esc behavior unchanged


# ── Unit Q (0.3.11): type-ahead queue + up-arrow history recall ───────────────
# Enter while a turn runs used to ERASE the typed line (buf.reset() before the
# busy gate). Now it queues (Claude-Code type-ahead): the line runs after the
# current turn, the toolbar shows the depth, and up-arrow recalls submitted
# lines to edit/resend. Handlers delegate to module-level *_action helpers —
# same DI testing philosophy as _escape_action/_interrupt_action above.

from collections import deque   # noqa: E402


class _RecBuf:
    """Buffer stand-in for _submit_line: records history appends."""
    def __init__(self, text=""):
        self.text = text
        class _H:
            def __init__(self): self.items = []
            def append_string(self, s): self.items.append(s)
        self.history = _H()


def test_enter_while_busy_queues_line_not_erased():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    assert _submit_line("deploy the fix", buf, pending, True, started.append) == "queued"
    assert list(pending) == ["deploy the fix"]      # preserved, NOT erased
    assert started == []                             # and NOT run mid-turn
    assert buf.history.items == ["deploy the fix"]   # recallable via up-arrow


def test_submit_while_idle_starts_turn_immediately():
    # Idle + non-empty = today's normal submit, byte-identical behavior.
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    assert _submit_line("hello", buf, pending, False, started.append) == "started"
    assert started == ["hello"] and not pending
    assert buf.history.items == ["hello"]            # typed turns are recallable too


def test_toolbar_shows_queue_depth_busy_and_idle():
    busy = _txt(build_toolbar("default", 0, 0, busy=True, elapsed=1.0, queued=2))
    assert "⋯2 queued" in busy and "working" in busy
    idle = _txt(build_toolbar("default", 0, 0, queued=1))
    assert "⋯1 queued" in idle and "Shift + TAB" in idle


def test_toolbar_hides_queue_segment_when_empty():
    assert "queued" not in _txt(build_toolbar("default", 0, 0, busy=True, elapsed=1.0))
    assert "queued" not in _txt(build_toolbar("default", 0, 0))


def test_toolbar_shows_reconnecting():
    # Stream transport down mid-turn (W1 front-5): the honest reconnecting
    # state replaces the busy spinner/working line entirely — no fake
    # "working" while the transport is actually down.
    frags = build_toolbar("default", 0, 0, busy=True, reconnecting=3)
    text = _txt(frags)
    assert "⟳ reconnecting (3)" in text and "working" not in text


def test_toolbar_reconnecting_only_applies_while_busy():
    # Idle never shows the reconnecting glyph — busy=False means no turn is
    # running to reconnect for.
    assert "reconnecting" not in _txt(build_toolbar("default", 0, 0, reconnecting=3))


def test_turn_completion_drains_oldest_queued_to_submit_path():
    from webbee.tui import _drain_pending
    started = []
    pending = deque(["first", "second"])
    assert _drain_pending(pending, started.append) is True
    assert started == ["first"]                      # OLDEST goes out…
    assert list(pending) == ["second"]               # …one per completion, rest stay


def test_drain_with_empty_queue_is_a_noop():
    from webbee.tui import _drain_pending
    started = []
    assert _drain_pending(deque(), started.append) is False
    assert started == []


def test_fifo_order_across_multiple_queued():
    from webbee.tui import _drain_pending, _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    for t in ("a", "b", "c"):
        _submit_line(t, buf, pending, True, started.append)
    assert started == []                             # nothing runs mid-turn
    while _drain_pending(pending, started.append):   # successive turn completions
        pass
    assert started == ["a", "b", "c"]                # strict FIFO


def test_up_arrow_recalls_last_submitted_line():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action, _submit_line

    async def scenario():
        buf = Buffer(multiline=False)
        _submit_line("fix the flaky test", buf, deque(), True, lambda t: None)
        buf.load_history_if_not_yet_loaded()   # BufferControl does this each repaint
        await buf._load_history_task           # loader fills the working lines
        event = _FakeEvent()
        sel = {"i": None}
        _arrow_up_action(event, buf, sel, 0, True)   # busy → history, never step-nav
        assert buf.text == "fix the flaky test"
        assert sel["i"] is None                       # step selection untouched
    asyncio.run(scenario())


def test_down_arrow_cycles_history_forward():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_down_action, _arrow_up_action

    async def scenario():
        buf = Buffer(multiline=False)
        buf.history.append_string("older")
        buf.history.append_string("newer")
        buf.load_history_if_not_yet_loaded()
        await buf._load_history_task
        event = _FakeEvent()
        _arrow_up_action(event, buf, {"i": None}, 0, True)
        _arrow_up_action(event, buf, {"i": None}, 0, True)
        assert buf.text == "older"
        _arrow_down_action(event, buf, {"i": None}, 0, True)
        assert buf.text == "newer"
    asyncio.run(scenario())


def test_up_arrow_with_text_present_pulls_history_not_steps():
    # The step-nav gate requires EMPTY input — with a draft in the box the
    # arrow edits history even when idle with steps present.
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action

    async def scenario():
        buf = Buffer(multiline=False)
        buf.history.append_string("submitted earlier")
        buf.text = "draf"
        buf.load_history_if_not_yet_loaded()
        await buf._load_history_task
        event = _FakeEvent()
        sel = {"i": None}
        _arrow_up_action(event, buf, sel, 3, False)   # idle + steps, but text present
        assert buf.text == "submitted earlier"
        assert sel["i"] is None                        # no step selection created
    asyncio.run(scenario())


def test_up_arrow_during_step_nav_still_navigates_steps():
    # Idle + empty input + steps present = EXACTLY today's step-navigation.
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_down_action, _arrow_up_action

    buf = Buffer(multiline=False)                     # empty input, no history touch
    sel = {"i": None}
    event = _FakeEvent()
    _arrow_up_action(event, buf, sel, 3, False)
    assert sel["i"] == 2 and buf.text == ""           # selection moves, no history pull
    _arrow_up_action(event, buf, sel, 3, False)
    assert sel["i"] == 1
    _arrow_down_action(event, buf, sel, 3, False)
    assert sel["i"] == 2
    assert event.app.invalidated is True


def test_dock_end_to_end_type_ahead_queues_then_drains_fifo():
    # Drive the REAL Application (pipe input + dummy output): Enter while a
    # turn runs queues the lines instead of erasing them, the drain in
    # _run_turn's finally submits them one per turn-completion, FIFO, through
    # the SAME on_line path a typed line takes; Ctrl-D exits when idle.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran = []
        gate = asyncio.Event()
        busy = {"v": False}

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink)

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])      # turn 1 is running
                pipe.send_text("second\r")                   # typed WHILE busy → queue
                pipe.send_text("third\r")
                await asyncio.sleep(0.1)
                assert ran == ["first"]                      # nothing runs mid-turn
                gate.set()                                   # let the turns finish
                await _until(lambda: ran == ["first", "second", "third"])   # FIFO
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                       # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── 0.3.12: the queue is VISIBLE + managed (0.3.13: visibility moved from the
# static `⋯ queued:` scrollback echo — which scrolled away, duplicated and went
# stale — into the LIVE queue panel above the input, webbee.queue_panel; the
# panel section below covers it). A drain still announces itself
# (sink.queued_run) right before the drained line's normal ❯ user-echo, the
# toolbar segment is an ACCENT (tb.working, not dim), /queue // /queue clear
# manage the shared deque even mid-turn, and a user STOP (Esc/Ctrl-C)
# PRESERVES the queue — drain on natural completion only.


def test_whitespace_never_queues():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    for junk in ("", "   ", "\t \t"):
        assert _submit_line(junk, buf, pending, True, started.append) == "ignored"
        assert _submit_line(junk, buf, pending, False, started.append) == "ignored"
    assert not pending and started == []
    assert buf.history.items == []                   # junk is not recallable either


def test_drain_marks_the_start_with_remaining_depth():
    from webbee.tui import _drain_pending
    started, marks = [], []
    pending = deque(["a", "b"])
    assert _drain_pending(pending, started.append, mark=marks.append) is True
    assert started == ["a"] and marks == [1]         # announced with what's left waiting
    assert _drain_pending(pending, started.append, mark=marks.append) is True
    assert started == ["a", "b"] and marks == [1, 0]


def test_drain_pending_requeues_on_start_error_and_survives_mark_error():
    # The popped line is already OUT of `pending` the instant it's read: a
    # `mark` blow-up (pure render-side announcement) must never lose it, and
    # a `start` blow-up must put it BACK at the head before propagating —
    # a broken start must not silently vanish a queued line.
    from webbee.tui import _drain_pending

    def bad_mark(n):
        raise RuntimeError

    def boom(t):
        raise RuntimeError

    q = deque(["a"])
    try:
        _drain_pending(q, boom, mark=bad_mark)
    except RuntimeError:
        pass
    assert list(q) == ["a"]           # nothing lost


def test_drain_pending_mark_error_alone_still_drains():
    from webbee.tui import _drain_pending
    started = []
    q = deque(["a", "b"])

    def bad_mark(n):
        raise RuntimeError

    assert _drain_pending(q, started.append, mark=bad_mark) is True
    assert started == ["a"] and list(q) == ["b"]      # the mark error was swallowed


def test_toolbar_queued_segment_is_accent_not_dim():
    for frags in (build_toolbar("default", 0, 0, busy=True, elapsed=1.0, queued=2),
                  build_toolbar("default", 0, 0, queued=2)):
        styles = [style for style, seg in frags if "queued" in seg]
        assert styles == ["class:tb.working"]        # pops in busy AND idle, never dim


def test_is_queue_command_matches_queue_and_subcommands_only():
    from webbee.tui import _is_queue_command
    assert _is_queue_command("/queue")
    assert _is_queue_command("  /QUEUE clear ")
    assert not _is_queue_command("/queues")
    assert not _is_queue_command("queue")
    assert not _is_queue_command("/status")
    assert not _is_queue_command("")


def test_escape_stop_flags_the_turn_so_the_queue_is_preserved():
    from webbee.tui import _escape_action
    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    _escape_action({"i": None}, turn, lambda: True, None, event, buf=_FakeBuf(""))
    assert task.cancelled == [1] and turn.get("stopped") is True


def test_ctrl_c_stop_flags_the_turn_so_the_queue_is_preserved():
    from webbee.tui import _interrupt_action
    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    _interrupt_action(turn, lambda: True, None, event)
    assert task.cancelled == [1] and turn.get("stopped") is True


def test_dock_stop_preserves_queue_then_natural_completion_drains():
    # THE rock-solid rule, end-to-end on the real Application: a user STOP
    # (Ctrl-C — deterministic in a pipe, no Esc flush timeout) never auto-runs
    # the queue; the queued line survives, and only the NEXT natural turn
    # completion drains it (with the queued_run marker).
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran, markers = [], []
        gate = asyncio.Event()
        busy = {"v": False}

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            try:
                await gate.wait()
            except asyncio.CancelledError:
                pass                    # repl._run_turn absorbs a user stop
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        pend = deque()
        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink, pending=pend)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None,
                    queued_run=markers.append))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])         # turn 1 is running
                pipe.send_text("queued follow-up\r")           # type-ahead while busy
                await _until(lambda: list(pend) == ["queued follow-up"])   # queued AT ONCE
                pipe.send_text("\x03")                         # user STOP (Ctrl-C)
                await _until(lambda: not busy["v"])
                await asyncio.sleep(0.1)
                assert ran == ["first"]                        # the queue did NOT auto-run
                assert list(pend) == ["queued follow-up"]      # preserved, still visible
                assert markers == []                           # and no drain marker fired
                gate.set()                                     # next turns complete at once
                pipe.send_text("resume\r")                     # a deliberate new turn
                await _until(lambda: ran == ["first", "resume", "queued follow-up"])
                assert markers == [0]                          # the drain announced itself
                assert not pend
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                         # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True
        assert "⋯ queued" not in pane.dump()      # the transcript stays CLEAN

    asyncio.run(scenario())


def test_closing_a_busy_tab_with_queued_lines_never_ghost_drains():
    # FIX2: closing a busy tab (Ctrl-W) with lines already queued must not
    # drain them into a brand-new turn -- the REAL webbee.repl._cancel_slot
    # (post-fix) flags turn["stopped"] before cancelling, mirroring a user
    # Esc/Ctrl-C stop, so tui's own drain rule holds: nothing starts anywhere,
    # and the queue simply disappears along with the slot it belonged to.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.repl import _cancel_slot
    from webbee.slots import SessionSlot, SlotManager

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        pane_a = tui.OutputPane(width=80)
        ran = []
        gate = asyncio.Event()
        busy = {"v": False}

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            try:
                await gate.wait()
            except asyncio.CancelledError:
                pass                    # repl._run_turn_on absorbs a cancel too
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink_a = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                                 consent_pending=lambda: False, resolve_consent=lambda t: None)
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=pane_a, sink=sink_a, agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.active_idx = 1

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None,
                    cancel_slot=_cancel_slot))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])              # turn running in A
                pipe.send_text("queued 1\r")
                pipe.send_text("queued 2\r")
                await _until(lambda: list(slot_a.pending) == ["queued 1", "queued 2"])

                pipe.send_text("\x17")                                # Ctrl-W: close busy tab A
                await _until(lambda: len(slots.slots) == 1)           # only Home left
                await _until(lambda: not busy["v"])
                await asyncio.sleep(0.1)                               # let any ghost drain surface

                assert ran == ["first"]                # nothing else ever ran -- no ghost turn
                assert list(slot_a.pending) == ["queued 1", "queued 2"]   # dies with the slot, untouched
                assert slot_a.turn.get("task") is None
                assert slots.active_idx == 0            # landed on Home

                pipe.send_text("\x04")                  # idle, no sessions -> exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_run_session_uses_caller_turn_dict():
    # The repl's poller gate (_poller_busy) reads slots.active().turn -- when
    # a caller-built slot carries its OWN turn dict object, run_session must
    # mutate THAT object, not a private one of its own.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        gate = asyncio.Event()
        busy = {"v": False}
        caller_turn = {"task": None}

        async def on_line(text, slot=None):
            busy["v"] = True
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink, turn=caller_turn)

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("hello\r")
                await _until(lambda: busy["v"])
                # the SAME dict object the caller holds now carries the live
                # task -- exactly what the repl's poller gate reads.
                assert caller_turn["task"] is not None
                assert not caller_turn["task"].done()
                gate.set()
                await _until(lambda: caller_turn["task"] is None)   # cleared on completion
                pipe.send_text("\x04")                              # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_error_turn_holds_queue():
    # THE other half of the drain rule (W1 task 6): an ERROR-terminated turn
    # must hold the queue exactly like a user stop does -- a broken backend
    # must never burn one queued line per failing turn. on_line completes
    # NORMALLY here (no exception escapes it, no turn["stopped"]) but
    # status()["turn_failed"] is True (repl's except branch calls
    # RichSink.mark_turn_failed(); this fake status mirrors that). Only the
    # NEXT clean completion (turn_failed back to False) drains.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran, markers = [], []
        gate = asyncio.Event()
        busy = {"v": False}
        failed = {"v": False}

        async def on_line(text, slot=None):
            busy["v"] = True
            failed["v"] = False            # begin_turn clears the one-turn flag
            ran.append(text)
            if text == "boom":
                await gate.wait()
                try:
                    raise OSError("network down")
                except OSError:
                    failed["v"] = True     # mark_turn_failed(): swallowed, never crashes the REPL
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False,
                    "turn_failed": failed["v"]}

        pend = deque()
        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink, pending=pend)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None,
                    queued_run=markers.append))
                await asyncio.sleep(0.05)
                pipe.send_text("boom\r")
                await _until(lambda: ran == ["boom"])          # the ERROR turn is running
                pipe.send_text("queued follow-up\r")           # type-ahead while it runs
                await _until(lambda: list(pend) == ["queued follow-up"])   # queued AT ONCE
                gate.set()                                     # let the error surface + get swallowed
                await _until(lambda: not busy["v"])            # the turn ends NATURALLY (no stop)
                await asyncio.sleep(0.1)
                assert ran == ["boom"]                          # the queue did NOT auto-run…
                assert list(pend) == ["queued follow-up"]      # …preserved, still visible
                assert markers == []                           # …and no drain marker fired
                pipe.send_text("resume\r")                      # a deliberate new (clean) turn
                await _until(lambda: ran == ["boom", "resume", "queued follow-up"])
                assert markers == [0]                           # clean completion DOES drain
                assert not pend
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                          # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_dock_queue_command_runs_immediately_even_while_busy():
    # /queue must manage the queue exactly when it matters — MID-TURN. It goes
    # straight to on_line as a background task (never type-ahead-queued), so
    # /queue clear typed while busy empties the shared deque before the turn
    # ends, and nothing drains at completion.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran = []
        gate = asyncio.Event()
        busy = {"v": False}
        pend = deque()

        async def on_line(text, slot=None):
            if text.split()[0].lower() == "/queue":      # the repl handler: display-only
                ran.append(text)
                if "clear" in text:
                    pend.clear()
                return
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink, pending=pend)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])
                pipe.send_text("second\r")                     # queued (type-ahead)
                await _until(lambda: list(pend) == ["second"])
                pipe.send_text("/queue\r")                     # runs NOW, while busy
                await _until(lambda: "/queue" in ran)
                assert list(pend) == ["second"]                # listing didn't touch it
                pipe.send_text("/queue clear\r")               # drops it, still mid-turn
                await _until(lambda: not pend)
                gate.set()                                     # turn 1 finishes naturally
                await _until(lambda: not busy["v"])
                await asyncio.sleep(0.1)
                assert "second" not in ran                     # cleared → nothing drained
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── 0.3.13: the LIVE queue panel + pull-to-edit ───────────────────────────────
# The static `⋯ queued:` scrollback echoes are GONE (they scrolled away,
# duplicated, and went stale when edited). The queue now lives in a panel
# pinned ABOVE the input (webbee.queue_panel — pure fragment/height builders
# mounted in a ConditionalContainer): it updates live on every add/edit/drain,
# ↑ on an empty input pulls the NEWEST item back into the box for editing
# (re-submit re-queues it), a click pulls THAT item, and the transcript stays
# real-turns-only.

from webbee.queue_panel import (  # noqa: E402
    QP_MAX_ITEMS, one_line, pull_item, queue_fragments, queue_height)


def _panel_text(frags):
    return "".join(f[1] for f in frags)


def test_empty_queue_renders_no_panel():
    assert queue_fragments(deque()) == []
    assert queue_height(deque()) == 0        # ConditionalContainer hides it anyway


def test_panel_header_counts_and_orders_items_drain_first_to_newest():
    frags = queue_fragments(deque(["first", "second"]))
    text = _panel_text(frags)
    assert "⋯ queued (2)" in text
    assert text.index("first") < text.index("second")   # top row drains next (FIFO)
    assert text.count("\n") == 2                         # header + exactly one row each
    assert not NO_CYRILLIC.search(text)


def test_panel_newest_item_accented_older_muted():
    frags = queue_fragments(deque(["older", "newest"]))
    assert [f[0] for f in frags if "older" in f[1]] == ["class:qp.item"]
    assert [f[0] for f in frags if "newest" in f[1]] == ["class:qp.last"]


def test_panel_caps_at_newest_five_with_a_more_row():
    items = [f"item{i}" for i in range(8)]
    frags = queue_fragments(deque(items))
    text = _panel_text(frags)
    assert "⋯ queued (8)" in text                        # header keeps the TRUE depth
    assert "… +3 more" in text                           # the oldest 3 hide behind it
    assert all(t in text for t in items[3:])             # newest 5 shown
    assert all(t not in text for t in items[:3])


def test_panel_height_is_header_plus_rows_capped():
    assert queue_height(deque(["a"])) == 2                       # header + 1
    assert queue_height(deque(["a"] * QP_MAX_ITEMS)) == 6        # header + 5
    assert queue_height(deque(["a"] * 9)) == 7                   # header + 5 + more-row


# ── W2 Task 5: proportional chrome — max_items overrides the fixed cap ─────
# The dock feeds sizing.panel_cap(rows) through this param so a tall terminal
# shows more queued rows and a short one shows fewer; QP_MAX_ITEMS stays the
# DEFAULT for every direct/test caller that doesn't pass one.

def test_queue_fragments_respects_max_items_param():
    items = [f"item{i}" for i in range(9)]
    frags = queue_fragments(deque(items), max_items=7)
    text = _panel_text(frags)
    assert "⋯ queued (9)" in text                        # header keeps the TRUE depth
    assert "… +2 more" in text                           # only the oldest 2 hide now
    assert all(t in text for t in items[2:])             # newest 7 shown
    assert all(t not in text for t in items[:2])
    # default (QP_MAX_ITEMS=5) is UNCHANGED when max_items isn't passed
    default_text = _panel_text(queue_fragments(deque(items)))
    assert "… +4 more" in default_text
    assert all(t in default_text for t in items[4:])


def test_queue_height_respects_max_items_param():
    items = ["a"] * 9
    assert queue_height(deque(items), max_items=7) == 1 + 7 + 1  # header + 7 + more-row
    assert queue_height(deque(items)) == 1 + QP_MAX_ITEMS + 1    # default untouched


def test_panel_item_is_one_truncated_row():
    frags = queue_fragments(deque(["x" * 500]), width=40)
    rows = _panel_text(frags).split("\n")
    assert len(rows) == 2                                # a huge item still costs ONE row
    assert rows[1].endswith("…") and len(rows[1]) <= 40


def test_panel_multiline_item_collapses_to_one_row():
    frags = queue_fragments(deque(["line1\nline2\tline3"]))
    text = _panel_text(frags)
    assert text.count("\n") == 1 and "line1 line2 line3" in text


def test_one_line_collapses_whitespace_and_truncates():
    assert one_line("a\n  b\t c", 80) == "a b c"
    assert one_line("abcdef", 4) == "abc…"
    assert one_line("abcd", 4) == "abcd"
    assert one_line("", 10) == ""
    assert one_line("abcdef", 0) == "abcdef"             # no width → no truncation


def test_panel_without_pull_has_no_mouse_handlers():
    assert all(len(f) == 2 for f in queue_fragments(deque(["a", "b"])))


def test_panel_item_mouse_up_pulls_that_item_other_events_fall_through():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    pulls = []
    frags = queue_fragments(deque(["a", "b", "c"]), pull=pulls.append, width=80)
    handlers = [f[2] for f in frags if len(f) == 3]
    assert len(handlers) == 3                            # every item row is clickable
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handlers[1](up) is None and pulls == [1]      # the CLICKED item, by index
    down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    assert handlers[0](down) is NotImplemented           # press/drag/wheel fall through
    assert pulls == [1]


def test_control_dispatches_click_row_to_the_matching_item():
    # prompt_toolkit's OWN per-fragment dispatch (FormattedTextControl.mouse_handler)
    # routes a click at row y to that row's handler — the exact path a real SGR
    # ?1000 button report takes after decoding. Row 0 = header (no handler).
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    pulls = []
    pend = deque(["alpha", "beta"])
    ctrl = FormattedTextControl(
        lambda: queue_fragments(pend, pull=pulls.append, width=80), focusable=False)
    ctrl.create_content(width=80, height=3)              # populates self._fragments
    ev = lambda y: MouseEvent(position=Point(4, y), event_type=MouseEventType.MOUSE_UP,
                              button=MouseButton.LEFT, modifiers=frozenset())
    assert ctrl.mouse_handler(ev(2)) is None and pulls == [1]    # row 2 = "beta"
    assert ctrl.mouse_handler(ev(1)) is None and pulls == [1, 0] # row 1 = "alpha"
    assert ctrl.mouse_handler(ev(0)) is NotImplemented           # header: no pull


# ── W1 front-3b task 11: click-to-collapse (header toggles to one row) ──────

def test_queue_collapsed_renders_single_header_row():
    q = deque(["a", "b", "c"])
    frags = queue_fragments(q, collapsed=True, toggle=lambda: None)
    assert len(frags) == 1 and "queued (3)" in frags[0][1] and "▸" in frags[0][1]
    assert len(frags[0]) == 3                      # header carries the toggle handler
    assert queue_height(q, collapsed=True) == 1


def test_queue_header_toggle_fires_on_mouse_up():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    hits = []
    frags = queue_fragments(deque(["a"]), collapsed=False, toggle=lambda: hits.append(1))
    handler = frags[0][2]
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(up) is None and hits == [1]
    scroll = MouseEvent(position=Point(0, 0), event_type=MouseEventType.SCROLL_UP,
                        button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(scroll) is NotImplemented


def test_pull_item_guards_draft_and_stale_index():
    # The ONE pull implementation behind BOTH ↑ and click: never clobbers a
    # typed draft; a stale index (queue drained between render and click) is
    # ignored; a valid pull moves the item out with the cursor at the end.
    from prompt_toolkit.buffer import Buffer
    buf = Buffer(multiline=False)
    buf.text = "half-typed draft"
    pend = deque(["a", "b"])
    assert pull_item(pend, buf, 1) is None                # draft protected
    assert buf.text == "half-typed draft" and list(pend) == ["a", "b"]
    buf.reset()
    assert pull_item(pend, buf, 5) is None                # stale index ignored
    assert pull_item(pend, buf, -1) is None
    assert pull_item(pend, buf, 0) == "a"                 # arbitrary index (click) — the item itself
    assert buf.text == "a" and buf.cursor_position == 1
    assert list(pend) == ["b"]


# ── W1 front-3b task 9: pull-to-edit keeps the original steer_iid ────────────
# A pull used to hand back a bare bool; now it hands back the REMOVED ITEM (or
# None) so the caller can read its carried QueuedLine.iid back out — the whole
# point being an UNCHANGED resubmit keeps the SAME iid (the kernel ring can
# then dedup a landed twin instead of running a genuine duplicate turn).

def test_pull_item_returns_the_item():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import QueuedLine
    buf = Buffer(multiline=False)
    q = deque([QueuedLine("do x", "iid-1")])
    item = pull_item(q, buf, 0)
    assert item == "do x" and item.iid == "iid-1" and not q


def test_rewrap_pulled_keeps_iid_when_unchanged_and_mints_when_edited():
    from webbee.tui import _rewrap_pulled
    pulled = {"text": "do x", "iid": "iid-1"}
    out = _rewrap_pulled(pulled, "do x")
    assert getattr(out, "iid", "") == "iid-1"
    assert pulled == {"text": "", "iid": ""}          # one-shot: consumed either way
    pulled = {"text": "do x", "iid": "iid-1"}
    out2 = _rewrap_pulled(pulled, "do x HARDER")
    assert getattr(out2, "iid", "") == ""              # edited ⇒ genuinely new message


def test_rewrap_pulled_is_a_noop_when_nothing_was_pulled():
    from webbee.tui import _rewrap_pulled
    pulled = {"text": "", "iid": ""}
    out = _rewrap_pulled(pulled, "typed fresh")
    assert out == "typed fresh" and getattr(out, "iid", "") == ""


def test_pull_then_resubmit_unchanged_keeps_the_original_iid_end_to_end():
    # The actual wiring _enter uses: _arrow_up_action records the pulled
    # item's text + iid into `pulled`; _rewrap_pulled hands the iid back
    # ONLY when the resubmitted text is byte-identical.
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import QueuedLine, _arrow_up_action, _rewrap_pulled
    buf = Buffer(multiline=False)
    pend = deque([QueuedLine("deploy the fix", "iid-42")])
    pulled = {"text": "", "iid": ""}
    _arrow_up_action(_FakeEvent(), buf, {"i": None}, 0, True, pend, pulled)
    assert buf.text == "deploy the fix" and not pend
    resubmitted = _rewrap_pulled(pulled, buf.text)          # unedited resubmit
    assert getattr(resubmitted, "iid", "") == "iid-42"      # SAME iid — kernel ring dedups
    # a second pull, this time edited before resubmit, must NOT carry the iid
    pend.append(QueuedLine("deploy the fix", "iid-42"))
    buf.reset()
    _arrow_up_action(_FakeEvent(), buf, {"i": None}, 0, True, pend, pulled)
    edited = _rewrap_pulled(pulled, buf.text + " NOW")
    assert getattr(edited, "iid", "") == ""                 # edited ⇒ no carried iid


def test_up_arrow_pulls_newest_queued_into_empty_buffer_busy_or_idle():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action
    for busy in (True, False):        # NOT busy-gated: the queue survives Esc into idle
        buf = Buffer(multiline=False)
        pend = deque(["older", "newest"])
        event = _FakeEvent()
        sel = {"i": None}
        _arrow_up_action(event, buf, sel, 0, busy, pend)
        assert buf.text == "newest"                      # the last thing queued
        assert buf.cursor_position == len("newest")      # cursor ready at the end
        assert list(pend) == ["older"]                   # it LEFT the queue
        assert sel["i"] is None and event.app.invalidated is True


def test_repeated_pulls_walk_newest_to_oldest():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action
    buf = Buffer(multiline=False)
    pend = deque(["a", "b", "c"])
    event = _FakeEvent()
    for expected in ("c", "b", "a"):
        buf.reset()                                      # emptied (submitted/cleared)…
        _arrow_up_action(event, buf, {"i": None}, 0, True, pend)
        assert buf.text == expected                      # …then the next-newest pulls
    assert not pend


def test_up_arrow_with_text_present_never_clobbers_the_draft():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action

    async def scenario():
        buf = Buffer(multiline=False)
        buf.history.append_string("submitted earlier")
        buf.text = "half-typed draft"
        buf.load_history_if_not_yet_loaded()
        await buf._load_history_task
        pend = deque(["queued line"])
        _arrow_up_action(_FakeEvent(), buf, {"i": None}, 0, True, pend)
        assert list(pend) == ["queued line"]             # NOT pulled — buffer has text
        assert buf.text == "submitted earlier"           # history served instead
    asyncio.run(scenario())


def test_pull_queued_takes_precedence_over_step_nav():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action
    buf = Buffer(multiline=False)
    pend = deque(["queued line"])
    sel = {"i": None}
    _arrow_up_action(_FakeEvent(), buf, sel, 3, False, pend)   # idle + steps + queue
    assert buf.text == "queued line" and not pend        # the queued text is fresher intent
    assert sel["i"] is None                              # steps reachable once queue empties


def test_dock_end_to_end_panel_pull_reedit_requeue_and_clean_transcript():
    # The full loop on the REAL Application: queue while busy → the item is in
    # the PANEL (queue_fragments over the shared deque), NOT in the scrollback;
    # ↑ pulls it into the input (it leaves the panel); Enter re-queues it at
    # the tail; the natural drain announces itself (queued_run) and empties the
    # panel; the transcript never saw a `⋯ queued` echo.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran, markers = [], []
        gate = asyncio.Event()
        busy = {"v": False}
        pend = deque()

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink, pending=pend)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None,
                    queued_run=markers.append))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])           # turn 1 is running
                pipe.send_text("second draft\r")                 # type-ahead → queue
                await _until(lambda: list(pend) == ["second draft"])
                assert "second draft" in _panel_text(queue_fragments(pend))  # in the PANEL
                assert "⋯ queued" not in pane.dump()             # NOT in the scrollback
                pipe.send_text("\x1b[A")                         # ↑ pulls it to edit
                await _until(lambda: not pend)                   # it LEFT the panel
                pipe.send_text(" v2\r")                          # edit + re-submit (busy)
                await _until(lambda: list(pend) == ["second draft v2"])   # re-queued at tail
                assert ran == ["first"]                          # still nothing mid-turn
                gate.set()                                       # natural completion
                await _until(lambda: ran == ["first", "second draft v2"])
                assert markers == [0]                            # drain announced itself
                await _until(lambda: not pend)                   # panel emptied
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                           # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True
        assert "⋯ queued" not in pane.dump()                     # transcript stayed CLEAN

    asyncio.run(scenario())


# ── 0.3.14: cross-surface (remote-queued) rows in the live panel ─────────────
# Full-queue-layer K1: a Telegram/panel follow-up queued into the RUNNING
# kernel session shows in the SAME live panel the instant the kernel announces
# it (task_queued) — rendered ABOVE the local rows (the kernel drains its own
# queue first, mid-run), tagged [origin], and DISPLAY-ONLY: never a mouse
# handler, never part of the ↑/click pull index space (you can't pull a
# kernel-queued item into the local input buffer).

def _remote(origin, text, iid=""):
    return {"origin": origin, "text": text, "iid": iid}


def test_remote_rows_render_above_local_tagged_by_origin():
    frags = queue_fragments(deque(["local line"]),
                            remote=[_remote("telegram", "fix the tests", "i1"),
                                    _remote("web-panel", "then the docs", "i2")])
    text = _panel_text(frags)
    assert "⋯ queued (3)" in text                       # header counts BOTH queues
    assert "[telegram] fix the tests" in text
    assert "[web-panel] then the docs" in text
    # remote rows sit ABOVE the local ones — top→bottom stays drain order
    assert text.index("[telegram]") < text.index("[web-panel]") < text.index("local line")
    assert not NO_CYRILLIC.search(text)


def test_remote_rows_are_display_only_never_pullable():
    pulls = []
    frags = queue_fragments(deque(["mine"]), pull=pulls.append, width=80,
                            remote=[_remote("telegram", "remote thing", "i1")])
    # every remote row is a plain 2-tuple (no mouse handler); local rows keep
    # their handler and their LOCAL index (remote rows never shift pull math)
    remote_frags = [f for f in frags if "[telegram]" in f[1]]
    assert remote_frags and all(len(f) == 2 for f in remote_frags)
    handlers = [f[2] for f in frags if len(f) == 3]
    assert len(handlers) == 1                           # only the local row is clickable
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    handlers[0](up)
    assert pulls == [0]                                 # pending[0] == "mine", not the remote row


def test_remote_only_queue_renders_panel_without_pull_hint():
    frags = queue_fragments(deque(), remote=[_remote("telegram", "go", "i1")])
    text = _panel_text(frags)
    assert "⋯ queued (1)" in text and "[telegram] go" in text
    assert "↑ edit last" not in text                    # nothing local to pull/edit
    assert all(len(f) == 2 for f in frags)              # nothing clickable at all


def test_remote_rows_styled_distinct_and_one_line_truncated():
    frags = queue_fragments(deque(), width=40,
                            remote=[_remote("telegram", "x" * 500, "i1")])
    row = [f for f in frags if "[telegram]" in f[1]][0]
    assert row[0] == "class:qp.remote"
    line = row[1].lstrip("\n")
    assert line.endswith("…") and len(line) <= 40       # one panel row, hard-capped


def test_remote_rows_cap_with_a_more_row_and_height_counts_both():
    rem = [_remote("telegram", f"r{i}", f"i{i}") for i in range(7)]
    frags = queue_fragments(deque(["a"]), remote=rem)
    text = _panel_text(frags)
    assert "… +2 more" in text                          # oldest 2 remote hide
    assert "[telegram] r6" in text and "[telegram] r1" not in text
    assert queue_height(deque(["a"]), rem) == 1 + (QP_MAX_ITEMS + 1) + 1  # hdr + remote(5+more) + local
    assert queue_height(deque(), [_remote("telegram", "go")]) == 2        # hdr + 1 remote
    assert queue_height(deque(), []) == 0               # both empty → hidden
    assert queue_height(deque(["a"]))  == 2             # remote omitted → exactly as before


def test_remote_parked_row_renders_with_pause_prefix():
    # W1 front-3b: a row that survived a marathon PARK is tagged parked=True
    # by RichSink.end_turn -- the panel must show it's still queued
    # server-side (not phantom) with a ⏸ prefix, distinct from a live row.
    parked = _remote("telegram", "do x", "i1")
    parked["parked"] = True
    frags = queue_fragments(deque(), remote=[parked, _remote("web-panel", "live one", "i2")])
    text = _panel_text(frags)
    assert "⏸ [telegram] do x" in text
    assert "[web-panel] live one" in text and "⏸ [web-panel]" not in text


# ── 0.3.15: mid-turn inject — Enter-while-busy FLIES into the RUNNING turn ────
# The type-ahead used to hold every busy-typed line client-side until turn end
# (_drain_pending), so a running marathon never saw it mid-turn. Now the enter
# handler routes the busy path through an inject launcher: _inject_or_queue
# mints the steer_iid, POSTs immediately (repl._inject_via_gateway → gateway
# /inject → kernel task_id-less new_task → K2 fly-in at the next brain step)
# and falls back to today's local queue — carrying the SAME iid so the kernel
# dedup ring drops the twin — only when the inject fails. Idle Enter and the
# no-launcher path (fallback loop) are byte-identical to before.

def test_enter_while_busy_with_inject_wired_flies_not_queues():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started, injected = [], []
    res = _submit_line("deploy the fix", buf, pending, True, started.append,
                       inject=injected.append)
    assert res == "injected"
    assert injected == ["deploy the fix"]            # flew to the launcher NOW
    assert not pending and started == []             # never held locally, never a new turn
    assert buf.history.items == ["deploy the fix"]   # ↑-recall unchanged


def test_idle_enter_with_inject_wired_still_starts_normally():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started, injected = [], []
    res = _submit_line("hello", buf, pending, False, started.append,
                       inject=injected.append)
    assert res == "started" and started == ["hello"]
    assert injected == [] and not pending            # idle path byte-identical


def test_inject_ok_is_kernel_owned_nothing_queued_locally():
    from webbee.tui import _inject_or_queue

    async def scenario():
        pending = deque()
        calls = []

        async def inject(text, iid):
            calls.append((text, iid))
            return True

        ok = await _inject_or_queue(inject, "fly this in", pending)
        assert ok is True
        assert not pending                            # kernel-owned — no local row
        (text, iid), = calls
        assert text == "fly this in"
        assert re.fullmatch(r"[0-9a-f]{32}", iid)     # uuid4 hex, minted at enqueue
    asyncio.run(scenario())


def test_inject_or_queue_reuses_carried_iid():
    # A QueuedLine (a pull-to-edit resubmitted unchanged, see _rewrap_pulled)
    # already carries the original steer_iid -- _inject_or_queue must fly it
    # under THAT id, not mint a fresh one, so the kernel ring can still dedup
    # a landed twin.
    from webbee.tui import QueuedLine, _inject_or_queue

    async def scenario():
        pending = deque()
        calls = []

        async def inject(text, iid):
            calls.append((text, iid))
            return True

        ok = await _inject_or_queue(inject, QueuedLine("t", "iid-9"), pending)
        assert ok is True
        (text, iid), = calls
        assert text == "t" and iid == "iid-9"         # carried, not a fresh uuid
    asyncio.run(scenario())


def test_inject_failure_falls_back_to_local_queue_with_same_iid():
    from webbee.tui import QueuedLine, _inject_or_queue

    async def scenario():
        for failing in (
            lambda text, iid: _false(),               # gateway said no / no session
            lambda text, iid: _boom(),                # network error raised
        ):
            pending = deque()
            seen = {}

            async def inject(text, iid, f=failing):
                seen["iid"] = iid
                return await f()

            ok = await _inject_or_queue(inject, "queued instead", pending)
            assert ok is False
            assert list(pending) == ["queued instead"]   # today's local fallback
            item = pending[0]
            assert isinstance(item, QueuedLine)
            # the SAME iid rides the fallback row → the turn-end drain re-submits
            # under it and the kernel ring dedups if the inject landed after all
            assert item.iid == seen["iid"]

    async def _false():
        return False

    async def _boom():
        raise RuntimeError("offline")

    asyncio.run(scenario())


def test_queued_line_is_a_plain_str_everywhere_it_flows():
    from webbee.tui import QueuedLine
    from webbee.queue_panel import one_line
    q = QueuedLine("fix the  tests", "i1")
    assert q == "fix the  tests" and isinstance(q, str)
    assert q.iid == "i1"
    assert one_line(q, 80) == "fix the tests"         # panel row unchanged
    assert getattr("plain", "iid", "") == ""          # a typed line has none


def test_dock_end_to_end_busy_enter_injects_ok_and_failure_falls_back():
    # Drive the REAL Application: while turn 1 runs, the first busy Enter
    # flies through the inject leg (nothing queued locally, no drain later);
    # the second busy Enter hits a failing inject and falls back to the local
    # queue, draining at turn end through the SAME on_line path — its
    # QueuedLine still carrying the minted iid.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran, injected = [], []
        pending = deque()
        gate = asyncio.Event()
        busy = {"v": False}

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        async def inject(text, iid, slot=None):
            injected.append((text, iid))
            return text == "flies in"                 # the second line fails

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink, pending=pending)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None,
                    inject=inject))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])          # turn 1 running
                pipe.send_text("flies in\r")                     # busy → inject ok
                await _until(lambda: len(injected) == 1)
                assert injected[0][0] == "flies in"
                assert re.fullmatch(r"[0-9a-f]{32}", injected[0][1])
                assert not pending                               # kernel-owned
                pipe.send_text("falls back\r")                   # busy → inject FAILS
                await _until(lambda: list(pending) == ["falls back"])
                fallback_iid = pending[0].iid
                assert injected[1][1] == fallback_iid            # same iid, both legs
                assert ran == ["first"]                          # nothing ran mid-turn
                gate.set()                                       # finish the turns
                await _until(lambda: ran == ["first", "falls back"])
                assert getattr(ran[1], "iid", "") == fallback_iid  # iid rode the drain
                await _until(lambda: not busy["v"] and not pending)
                pipe.send_text("\x04")                           # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_launch_inject_posts_into_the_slot_captured_at_keypress_not_whatever_is_active_later():
    # FIX7a: the mid-turn inject fly-in's POST target is the slot CAPTURED
    # SYNCHRONOUSLY at Enter keypress time (tui._launch_inject) -- a switch
    # sent back-to-back right after (no await in between, so both land in
    # the pipe's buffer and get dispatched in the SAME processing burst,
    # before the scheduled `_inject_or_queue` background task's body has any
    # chance to run) must never redirect the POST to whatever tab is active
    # by the time that task's body actually calls `inject(text, iid, slot)`.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane_a, pane_b = tui.OutputPane(width=80), tui.OutputPane(width=80)
        busy = {"v": False}
        ran = []
        turn_gate = asyncio.Event()

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            await turn_gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink_a = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                                 consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane_a, sink=sink_a)
        slot_a = slots.slots[0]
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=pane_b, sink=_idle_sink(), agent=None)
        slots.add(slot_b)

        calls = []

        async def inject(text, iid, slot):
            calls.append((text, iid, slot))
            return True

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None, inject=inject))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")                       # starts a turn IN A
                await _until(lambda: ran == ["first"])

                pipe.send_text("fly this in\r")                 # busy(A) -> mid-turn inject launcher
                pipe.send_text("\x1b1")                          # Alt+1 (switch to B) -- SAME burst, no await
                await _until(lambda: calls != [])

                assert len(calls) == 1
                text, iid, slot = calls[0]
                assert text == "fly this in"
                assert slot is slot_a                               # the CAPTURED slot, never B

                turn_gate.set()
                await _until(lambda: not busy["v"])

                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 1)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_dock_mounts_sticky_todo_panel_above_queue_panel():
    # The layout contract: root HSplit = [pane, TODO panel, QUEUE panel,
    # input, toolbar]. Both are ConditionalContainers occupying ZERO rows when
    # empty (pixel-identical empty dock); the todo panel shows the moment the
    # sink-owned list fills, updates height in place, and STAYS visible while
    # the queue panel independently tracks the pending deque.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import ConditionalContainer
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        pane = tui.OutputPane(width=80)
        todos, pending = [], deque()

        def status():
            return {"tokens": 0, "credits": 0, "busy": False, "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        async def on_line(text, slot=None): ...

        sink = SimpleNamespace(status=status, is_busy=lambda: False,
                               consent_pending=lambda: False, resolve_consent=lambda t: None,
                               current_todos=todos)
        slots = mk_slots(pane=pane, sink=sink, pending=pending)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                conds = [c for c in get_app().layout.container.children
                         if isinstance(c, ConditionalContainer)]
                assert len(conds) == 2
                todo_cc, queue_cc = conds                # todo mounts ABOVE queue
                assert not todo_cc.filter() and not queue_cc.filter()   # empty dock
                todos.append({"content": "fix the bug", "status": "in_progress"})
                assert todo_cc.filter() and not queue_cc.filter()       # todo only
                assert todo_cc.content.height() == 2     # header + the ▶ row
                todos.append({"content": "run tests", "status": "pending"})
                assert todo_cc.content.height() == 3     # updates in place
                pending.append("queued line")
                assert queue_cc.filter()                 # queue joins independently
                todos.clear()
                assert not todo_cc.filter()              # /clear empties → hidden
                pipe.send_text("\x04")                   # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── W4a Task 3: run_session takes the SlotManager -- every internal closure
# resolves slots.active() AT CALL TIME (map §1/§2), and a turn's own dict/
# queue-drain/failure-read stay PINNED to the slot it started in even across
# a mid-turn tab switch. No tab-bar UI yet (Task 4) -- these tests switch
# tabs directly via `slots.active_idx`, exactly as a future keybinding will.

def test_switching_tabs_preserves_the_draft_and_restores_it_on_return():
    # 0.3.24 (per-tab drafts, product decision -- supersedes FIX7b's "drafts
    # dropped on switch"): a draft mid-type belongs to the tab you typed it
    # into, browser-tab style -- it must survive a switch AWAY and come back
    # verbatim (text + cursor) on a switch BACK, while the tab you switched
    # INTO in between never sees it.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot

    async def scenario():
        pane_a, pane_b = tui.OutputPane(width=80), tui.OutputPane(width=80)
        ran = []

        async def on_line(text, slot=None):
            ran.append(text)

        slots = mk_slots(pane=pane_a, sink=_idle_sink())
        slots.add(SessionSlot(kind="session", workspace=".", label="b",
                              pane=pane_b, sink=_idle_sink(), agent=None))

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("partial draft")          # no Enter -- just a draft in progress
                await asyncio.sleep(0.05)
                get_app().current_buffer.cursor_position = 7   # mid-text, not just "at the end"

                pipe.send_text("\x1b1")                    # Alt+1 -- switch to B
                await _until(lambda: slots.active_idx == 1)
                assert get_app().current_buffer.text == ""      # B never sees A's draft

                pipe.send_text("\r")                        # Enter on B's (genuinely empty) buffer
                await asyncio.sleep(0.1)
                assert ran == []                             # nothing resubmitted on B

                pipe.send_text("\x1b0")                      # Alt+0 -- switch back to A
                await _until(lambda: slots.active_idx == 0)
                assert get_app().current_buffer.text == "partial draft"   # restored verbatim
                assert get_app().current_buffer.cursor_position == 7      # cursor restored too

                # Exit via B -- idx 0 is structurally unclosable in this
                # 2-slot fixture (SlotManager.close's own Home-guard treats
                # index 0 as never closable, session or not), same escape
                # route the pre-0.3.24 version of this test used.
                pipe.send_text("\x1b1")
                await _until(lambda: slots.active_idx == 1)
                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 1)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_home_draft_is_isolated_from_a_session_tabs_draft():
    # Companion to the round-trip test above, specifically for Home: typing
    # on Home (never submitted -- home_input owns plain text there, no
    # Enter needed for this test) must not leak into a session tab's buffer,
    # and Home's own draft must survive a round trip exactly like a session
    # tab's does.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="a",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 0

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None,
                    home_input=lambda text: None))
                await asyncio.sleep(0.05)
                pipe.send_text("home draft")
                await asyncio.sleep(0.05)

                pipe.send_text("\x1b1")                    # Alt+1 -- switch to the session tab
                await _until(lambda: slots.active_idx == 1)
                assert get_app().current_buffer.text == ""      # session tab never sees Home's draft

                pipe.send_text("session draft")
                await asyncio.sleep(0.05)

                pipe.send_text("\x1b0")                      # Alt+0 -- back to Home
                await _until(lambda: slots.active_idx == 0)
                assert get_app().current_buffer.text == "home draft"   # Home's OWN draft restored

                pipe.send_text("\x1b1")                      # back to the session tab
                await _until(lambda: slots.active_idx == 1)
                assert get_app().current_buffer.text == "session draft"   # its own draft, untouched

                pipe.send_text("\x1b0")
                await _until(lambda: slots.active_idx == 0)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_submitting_a_line_clears_that_slots_draft_so_it_never_resurrects():
    # 0.3.24: a genuine submit must retire the stashed draft too -- otherwise
    # switching away and back after sending a message would restore text
    # that was already sent, silently resurrecting it in the input box.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot

    async def scenario():
        pane_a, pane_b = tui.OutputPane(width=80), tui.OutputPane(width=80)
        ran = []

        async def on_line(text, slot=None):
            ran.append(text)

        slots = mk_slots(pane=pane_a, sink=_idle_sink())
        slots.add(SessionSlot(kind="session", workspace=".", label="b",
                              pane=pane_b, sink=_idle_sink(), agent=None))

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("go\r")                       # type + submit, idle slot -> starts a turn
                await _until(lambda: ran == ["go"])

                pipe.send_text("\x1b1")                        # switch away
                await _until(lambda: slots.active_idx == 1)
                pipe.send_text("\x1b0")                        # ...and back
                await _until(lambda: slots.active_idx == 0)
                assert get_app().current_buffer.text == ""      # NOT resurrected

                pipe.send_text("\x1b1")
                await _until(lambda: slots.active_idx == 1)
                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 1)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_switch_preserves_the_leaving_slots_pulled_carry_so_resubmit_unchanged_still_dedups():
    # 0.3.24 (product decision, supersedes FIX7b's "pulled cleared on the
    # way out"): pulled now travels WITH the draft, on its own slot -- a
    # pulled-but-not-yet-resubmitted queue item survives a switch away and
    # back, and resubmitting it UNCHANGED after the round trip still reuses
    # its ORIGINAL steer_iid, so the kernel's dedup ring can still catch a
    # landed twin exactly as if the user had never glanced at another tab.
    # `_rewrap_pulled`'s one-shot consume still retires it -- on the Enter
    # that actually resubmits, not on the switch.
    from collections import deque

    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot
    from webbee.tui import QueuedLine

    async def scenario():
        pane_a, pane_b = tui.OutputPane(width=80), tui.OutputPane(width=80)
        ran = []

        async def on_line(text, slot=None):
            ran.append(text)

        pending_a = deque([QueuedLine("queued text", "iid-1")])
        slots = mk_slots(pane=pane_a, sink=_idle_sink(), pending=pending_a)
        slot_a = slots.slots[0]
        slots.add(SessionSlot(kind="session", workspace=".", label="b",
                              pane=pane_b, sink=_idle_sink(), agent=None))

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("\x1b[A")                     # Up -- pulls "queued text" into the input
                await asyncio.sleep(0.05)
                assert slot_a.pulled["iid"] == "iid-1"        # armed

                pipe.send_text("\x1b1")                        # Alt+1 -- switch away WITHOUT resubmitting
                await _until(lambda: slots.active_idx == 1)
                assert slot_a.pulled == {"text": "queued text", "iid": "iid-1"}   # SURVIVES the switch
                assert slot_a.draft == "queued text"                              # draft travels with it

                pipe.send_text("\x1b0")                        # Alt+0 -- switch back to A
                await _until(lambda: slots.active_idx == 0)
                assert get_app().current_buffer.text == "queued text"   # restored, unedited

                pipe.send_text("\r")                            # resubmit UNCHANGED -- no retyping
                await _until(lambda: ran == ["queued text"])
                assert getattr(ran[0], "iid", "") == "iid-1"    # ORIGINAL iid preserved -- dedup intact
                assert slot_a.pulled == {"text": "", "iid": ""}  # one-shot: consumed by THIS Enter, not the switch

                pipe.send_text("\x1b1")                          # switch to B -- A (idx 0) is unclosable here
                await _until(lambda: slots.active_idx == 1)
                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 1)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_swap_history_creates_and_repoints_per_slot_history():
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.history import InMemoryHistory

    from webbee.slots import SessionSlot
    from webbee.tui import _swap_history

    buf = Buffer(multiline=False)
    slot = SessionSlot(kind="session", workspace=".", label="a", pane=None, sink=None, agent=None)
    assert slot.history is None
    _swap_history(buf, slot)
    assert isinstance(slot.history, InMemoryHistory)
    assert buf.history is slot.history
    created = slot.history

    other_buf = Buffer(multiline=False)
    _swap_history(other_buf, slot)          # a second touch on the SAME slot reuses it
    assert slot.history is created
    assert other_buf.history is created


def test_enter_resolves_consent_on_the_active_slot_only():
    # Two slots, both with a consent armed on their OWN sink: Enter must
    # resolve whichever slot is ACTIVE -- the other slot's consent future
    # stays untouched even though both are "pending" at once (a background
    # slot's own ask_consent arms independently -- map §4 consent landmine).
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        def status():
            return {"tokens": 0, "credits": 0, "busy": False, "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": True}

        resolved_a, resolved_b = [], []
        sink_a = SimpleNamespace(status=status, is_busy=lambda: False,
                                 consent_pending=lambda: True,
                                 resolve_consent=resolved_a.append)
        sink_b = SimpleNamespace(status=status, is_busy=lambda: False,
                                 consent_pending=lambda: True,
                                 resolve_consent=resolved_b.append)
        pane_a, pane_b = tui.OutputPane(width=80), tui.OutputPane(width=80)
        slots = mk_slots(pane=pane_a, sink=sink_a)
        slots.add(SessionSlot(kind="session", workspace=".", label="b",
                              pane=pane_b, sink=sink_b, agent=None))

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("yes\r")
                await _until(lambda: resolved_a == ["yes"])
                assert resolved_b == []                      # B's own consent untouched

                slots.active_idx = 1                         # switch to B (no tab-bar UI yet)
                pipe.send_text("also yes\r")
                await _until(lambda: resolved_b == ["also yes"])
                assert resolved_a == ["yes"]                  # A untouched by the second Enter

                sink_a.consent_pending = lambda: False
                sink_b.consent_pending = lambda: False
                # Two session tabs, no Home in this fixture (it predates
                # Task 4's Home concept): Ctrl-D now closes the ACTIVE tab
                # first (Task 5 policy: session_count() > 1) instead of
                # exiting outright -- the first press closes B and lands
                # back on A, the second exits cleanly once only one tab
                # (idle) is left.
                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 1)
                pipe.send_text("\x04")                        # Ctrl-D exit (idle, single tab)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_turn_pinned_to_originating_slot_drains_its_own_queue_after_switch():
    # A turn started in slot A keeps draining A's OWN queue after the user
    # switches to slot B mid-turn -- the pinned `slot` _run_turn captured at
    # start time, not whatever's active when the turn actually finishes.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane_a, pane_b = tui.OutputPane(width=80), tui.OutputPane(width=80)
        ran = []
        gate = asyncio.Event()
        busy_a = {"v": False}

        async def on_line(text, slot=None):
            busy_a["v"] = True
            ran.append(text)
            await gate.wait()
            busy_a["v"] = False

        def status_a():
            return {"tokens": 0, "credits": 0, "busy": busy_a["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        def status_b():
            return {"tokens": 0, "credits": 0, "busy": False, "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink_a = SimpleNamespace(status=status_a, is_busy=lambda: busy_a["v"],
                                 consent_pending=lambda: False, resolve_consent=lambda t: None)
        sink_b = SimpleNamespace(status=status_b, is_busy=lambda: False,
                                 consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane_a, sink=sink_a)
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=pane_b, sink=sink_b, agent=None)
        slots.add(slot_b)
        slot_a = slots.slots[0]

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")                       # starts a turn IN A (A active)
                await _until(lambda: ran == ["first"])
                pipe.send_text("queued-in-a\r")                 # busy(A) -> queues into A's own deque
                await _until(lambda: list(slot_a.pending) == ["queued-in-a"])

                slots.active_idx = 1                            # switch to B mid-turn
                assert not slot_b.pending and slot_b.turn["task"] is None

                gate.set()                                       # A's turn completes naturally
                await _until(lambda: ran == ["first", "queued-in-a"])   # A's OWN queue drained
                assert not slot_a.pending
                assert not slot_b.pending                         # B never touched by A's drain
                await _until(lambda: not busy_a["v"])

                # Two session tabs, no Home here either -- index 0 (A) is
                # unconditionally guarded by SlotManager.close (the real
                # Home-at-0 invariant), so the close has to fire FROM B
                # (still active): first Ctrl-D closes B and lands on A,
                # second Ctrl-D exits cleanly (idle, single tab left).
                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 1)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_on_line_receives_the_pinned_slot_never_whatever_becomes_active_later():
    # FIX1 (W4a final review): on_line's CONTRACT is now on_line(text, slot)
    # -- the dock must thread the slot a turn started in through EVERY call,
    # so repl's _handle/_run_turn can act on it explicitly instead of
    # re-resolving slots.active() when their background task's body actually
    # runs. Companion to test_turn_pinned_to_originating_slot_drains_its_own_
    # queue_after_switch above (which only proves TUI's OWN turn-task/queue
    # stay pinned to slot A) -- this asserts the SLOT ARGUMENT itself, the
    # thing repl needs to kill its internal active() resolution.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane_a, pane_b = tui.OutputPane(width=80), tui.OutputPane(width=80)
        calls = []
        gate = asyncio.Event()
        busy_a = {"v": False}

        async def on_line(text, slot):
            calls.append((text, slot))
            busy_a["v"] = True
            await gate.wait()
            busy_a["v"] = False

        def status_a():
            return {"tokens": 0, "credits": 0, "busy": busy_a["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink_a = SimpleNamespace(status=status_a, is_busy=lambda: busy_a["v"],
                                 consent_pending=lambda: False, resolve_consent=lambda t: None)
        sink_b = _idle_sink()
        slots = mk_slots(pane=pane_a, sink=sink_a)
        slot_a = slots.slots[0]
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=pane_b, sink=sink_b, agent=None)
        slots.add(slot_b)

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")                       # starts a turn IN A (A active)
                await _until(lambda: calls == [("first", slot_a)])
                pipe.send_text("queued-in-a\r")                 # busy(A) -> queues into A's own deque
                await _until(lambda: list(slot_a.pending) == ["queued-in-a"])

                slots.active_idx = 1                            # switch to B mid-turn -- direct,
                                                                 # deterministic (no key/race needed)
                gate.set()                                       # A's turn completes naturally
                await _until(lambda: calls == [("first", slot_a), ("queued-in-a", slot_a)])
                # BOTH calls carried slot_a -- the drained line's on_line call
                # was never handed slot_b, even though B is active right now.
                assert not slot_b.pending and slot_b.turn["task"] is None

                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 1)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_home_active_enter_routes_plain_text_to_home_input_not_on_line():
    # Home (sink=None) has no busy/queue semantics: a slash command still
    # reaches on_line like every other slot, but plain text routes to
    # home_input (Task 6 wires a real one; None just means "ignored") and
    # NEVER to on_line.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        home = SessionSlot(kind="home", workspace=".", label="Home", pane=pane, sink=None, agent=None)
        slots = SlotManager()
        slots.add(home)

        on_line_calls, home_calls = [], []

        async def on_line(text, slot=None):
            on_line_calls.append(text)

        async def home_input(text):
            home_calls.append(text)

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None,
                    home_input=home_input))
                await asyncio.sleep(0.05)
                pipe.send_text("hello there\r")
                await _until(lambda: home_calls == ["hello there"])
                assert on_line_calls == []                       # never routed to on_line

                pipe.send_text("/help\r")                         # a slash command still goes to on_line
                await _until(lambda: on_line_calls == ["/help"])
                assert home_calls == ["hello there"]              # unchanged

                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_home_active_enter_with_no_home_input_ignores_plain_text():
    # home_input=None (the repl's Task 3 wiring, before Task 6) must never
    # crash -- a plain line on Home is simply dropped.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        home = SessionSlot(kind="home", workspace=".", label="Home", pane=pane, sink=None, agent=None)
        slots = SlotManager()
        slots.add(home)

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("hello there\r")
                await asyncio.sleep(0.1)                          # must not raise
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_home_input_first_turn_task_is_visible_and_esc_cancels_it():
    # FIX3: _home_input must route the first turn through the dock's own
    # start-turn seam (ui_hooks["start_turn_in"]) -- without it the
    # Home-spawned first turn ran as a bare await, invisibly: new_slot.
    # turn["task"] stayed None forever, so _busy_live() (and thus Esc/Ctrl-C)
    # never saw it as a live turn to cancel.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.repl import _home_input
    from webbee.slots import SessionSlot, SlotManager, WorkspaceResources

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        slots = SlotManager()
        slots.add(home)

        gate = asyncio.Event()
        busy = {"v": False}
        ran = []

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        new_sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                                   consent_pending=lambda: False, resolve_consent=lambda t: None,
                                   user_echo=lambda t: None)

        async def fake_make_session_slot(cfg, tp, ws, mode, *, resources, shared_client,
                                          agent_factory, intel_factory, shadow_factory, first):
            return SessionSlot(kind="session", workspace=ws, label="new",
                               pane=tui.OutputPane(width=80), sink=new_sink, agent=None)

        import webbee.repl as repl_mod
        orig = repl_mod._make_session_slot
        repl_mod._make_session_slot = fake_make_session_slot

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            try:
                await gate.wait()
            except asyncio.CancelledError:
                pass
            busy["v"] = False

        async def never_called(slot, text):
            raise AssertionError("fallback run_turn must not fire when a dock is present")

        ui_hooks = {}

        def home_input(text):
            return _home_input(
                text, slots=slots, cfg=None, token_provider=None, mode="default",
                resources=WorkspaceResources(), shared_client=None, agent_factory=None,
                intel_factory=None, shadow_factory=None, workspace=".",
                ui_hooks=ui_hooks, run_turn=never_called)

        try:
            with create_pipe_input() as pipe:
                with create_app_session(input=pipe, output=DummyOutput()):
                    task = asyncio.create_task(tui.run_session(
                        slots=slots, on_line=on_line, on_cycle=lambda: None,
                        home_input=home_input, ui_hooks=ui_hooks))
                    await asyncio.sleep(0.05)
                    pipe.send_text("build a thing\r")
                    await _until(lambda: ran == ["build a thing"])

                    assert len(slots.slots) == 2
                    new_slot = slots.slots[1]
                    assert slots.active_idx == 1
                    # THE fix: turn["task"] is populated -- not just "some
                    # coroutine happens to be running, unobserved by anyone".
                    live_task = new_slot.turn.get("task")
                    assert live_task is not None
                    assert not live_task.done()

                    pipe.send_text("\x1b")                     # lone Esc -- busy, so it stops the turn
                    # Esc found + cancelled the (now-visible) turn task: the
                    # gated on_line's own except-CancelledError branch runs,
                    # clearing busy -- before FIX3 turn["task"] stayed None
                    # forever, so _busy_live() was always False and this Esc
                    # would have taken the idle (step-clear) branch instead,
                    # never touching the turn at all.
                    await _until(lambda: not busy["v"], timeout=3.0)
                    assert live_task.cancelled() or live_task.done()
                    assert ran == ["build a thing"]             # nothing else ever ran
                    assert new_slot.turn.get("task") is None    # cleared by the finally block

                    pipe.send_text("\x04")                      # idle, single session -> exit
                    ok = await asyncio.wait_for(task, 5)
            assert ok is True
        finally:
            repl_mod._make_session_slot = orig

    asyncio.run(scenario())


# ── attach-on-poll: ui_hooks["start_attach_in"] -- Esc cancels an in-flight
# attach turn exactly like a typed one ──────────────────────────────────────

def test_start_attach_in_task_is_visible_and_esc_cancels_it():
    # Mirrors test_home_input_first_turn_task_is_visible_and_esc_cancels_it:
    # an attach turn (poll_idle_steer's attach_turn seam, repl._attach_turn_on)
    # must be genuinely tracked in slot.turn["task"] under a dock -- otherwise
    # _busy_live() never sees it as live and Esc/Ctrl-C can't cancel it.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        gate = asyncio.Event()
        busy = {"v": False}
        ran = []
        cancelled = {"v": False}

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        session = SessionSlot(kind="session", workspace=".", label="proj",
                              pane=tui.OutputPane(width=80), sink=sink, agent=None)
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1

        async def _attach_coro():
            # Mirrors what repl._attach_turn_on's own _drive() does around
            # slot.agent.attach(): flips the sink busy while "running" the
            # attach turn, absorbs the Esc cancel like any other turn.
            busy["v"] = True
            ran.append("attaching")
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise
            finally:
                busy["v"] = False

        async def on_line(text, slot=None):
            raise AssertionError("no typed line in this scenario -- attach is external")

        ui_hooks = {}

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None, ui_hooks=ui_hooks))
                await asyncio.sleep(0.05)

                # The seam repl's attach_turn wiring uses under a dock.
                start_attach_in = ui_hooks["start_attach_in"]
                live_task = start_attach_in(session, _attach_coro())
                await _until(lambda: ran == ["attaching"])

                assert session.turn.get("task") is live_task
                assert not live_task.done()

                pipe.send_text("\x1b")   # lone Esc -- busy, so it stops the turn
                await _until(lambda: cancelled["v"], timeout=3.0)
                assert live_task.cancelled() or live_task.done()
                assert session.turn.get("task") is None   # cleared by _finish_natural_turn

                pipe.send_text("\x04")   # idle, single session -> exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── W4a Task 4: the tab bar mounts at the TOP of the root layout ────────────

def test_tab_bar_window_is_root_hsplit_first_child():
    # Layout contract update (was: root = [pane, TODO, QUEUE, input, toolbar]
    # -- see test_dock_mounts_sticky_todo_panel_above_queue_panel): the tab
    # bar is now pinned ABOVE everything else, a plain always-visible Window
    # (not a ConditionalContainer -- unlike the queue/todo panels it never
    # hides), rendering Home + every session tab live off the SlotManager.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import ConditionalContainer, Window
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        def status():
            return {"tokens": 0, "credits": 0, "busy": False, "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        home_pane, session_pane = tui.OutputPane(width=80), tui.OutputPane(width=80)
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=home_pane, sink=None, agent=None)
        sink = SimpleNamespace(status=status, is_busy=lambda: False,
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        session = SessionSlot(kind="session", workspace=".", label="proj",
                              pane=session_pane, sink=sink, agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                children = get_app().layout.container.children
                bar = children[0]
                assert isinstance(bar, Window)
                assert not isinstance(bar, ConditionalContainer)   # ALWAYS visible
                text = "".join(f[1] for f in bar.content.text())
                assert "◆ Home" in text                            # Home always first
                assert "● 1·proj" in text                          # active session, marked ●N
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── 0.3.25 (Valentin, live screenshot review): tab bar polish ───────────────

def test_tab_bar_carries_the_tabbar_background_style():
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        slots = mk_slots(pane=tui.OutputPane(width=80), sink=_idle_sink())

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                bar = get_app().layout.container.children[0]
                assert bar.style == "class:tabbar"
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_root_layout_has_a_bare_spacer_row_right_after_the_tab_bar():
    # ONE blank breathing-room row between the tab bar and the transcript --
    # a plain Window (no style at all, never a ConditionalContainer -- it's
    # unconditional, unlike the todo/queue panels further down).
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import ConditionalContainer, Window
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        slots = mk_slots(pane=tui.OutputPane(width=80), sink=_idle_sink())

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                children = get_app().layout.container.children
                spacer = children[1]
                assert isinstance(spacer, Window)
                assert not isinstance(spacer, ConditionalContainer)
                assert spacer.style == ""            # no style at all -- bare terminal bg
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── W4a Task 5: tab keys + commands + lifecycle ─────────────────────────────
# Pure decision helpers first (same DI-testing philosophy as _escape_action),
# then end-to-end dock coverage for Ctrl-T, Alt+N, Ctrl-W, Ctrl-D and the
# ui_hooks seam repl's /tab, /new, /close route through.

def test_can_close_tab_true_on_empty_buffer_session_slot():
    from webbee.slots import SessionSlot
    from webbee.tui import _can_close_tab
    slot = SessionSlot(kind="session", workspace=".", label="t", pane=None, sink=None, agent=None)
    assert _can_close_tab(_FakeBuf(""), slot) is True


def test_can_close_tab_false_with_draft_text():
    from webbee.slots import SessionSlot
    from webbee.tui import _can_close_tab
    slot = SessionSlot(kind="session", workspace=".", label="t", pane=None, sink=None, agent=None)
    assert _can_close_tab(_FakeBuf("still typing"), slot) is False


def test_can_close_tab_false_on_home():
    from webbee.slots import SessionSlot
    from webbee.tui import _can_close_tab
    slot = SessionSlot(kind="home", workspace=".", label="Home", pane=None, sink=None, agent=None)
    assert _can_close_tab(_FakeBuf(""), slot) is False


def test_should_close_on_eof_true_with_more_than_one_session_active():
    from webbee.slots import SessionSlot
    from webbee.tui import _should_close_on_eof
    slots = mk_slots()
    slots.add(SessionSlot(kind="session", workspace=".", label="b", pane=None, sink=None, agent=None))
    slots.active_idx = 1
    assert _should_close_on_eof(slots) is True


def test_should_close_on_eof_false_with_a_single_session():
    from webbee.tui import _should_close_on_eof
    slots = mk_slots()
    assert _should_close_on_eof(slots) is False


def test_should_close_on_eof_false_on_home_even_with_other_sessions_open():
    from webbee.slots import SessionSlot, SlotManager
    from webbee.tui import _should_close_on_eof
    slots = SlotManager()
    slots.add(SessionSlot(kind="home", workspace=".", label="Home", pane=None, sink=None, agent=None))
    slots.add(SessionSlot(kind="session", workspace=".", label="a", pane=None, sink=None, agent=None))
    slots.add(SessionSlot(kind="session", workspace=".", label="b", pane=None, sink=None, agent=None))
    slots.active_idx = 0
    assert _should_close_on_eof(slots) is False


def _idle_status():
    return {"tokens": 0, "credits": 0, "busy": False, "current": "",
            "elapsed": 0.0, "tools": 0, "consent": False}


def _idle_sink(**extra):
    return SimpleNamespace(status=_idle_status, is_busy=lambda: False,
                           consent_pending=lambda: False, resolve_consent=lambda t: None, **extra)


async def _until(pred, timeout=5.0):
    import time
    t0 = time.time()
    while not pred():
        assert time.time() - t0 < timeout, "timed out"
        await asyncio.sleep(0.01)


def test_ctrl_t_jumps_to_home_from_a_session_tab():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="proj",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("\x14")                    # Ctrl-T
                await _until(lambda: slots.active_idx == 0)
                pipe.send_text("\x04")                     # single session left -> idle exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_alt_1_switches_from_home_to_session_tab_and_swaps_history():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="proj",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 0
        assert session.history is None

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("\x1b1")                    # Alt+1 -- both bytes together
                await _until(lambda: slots.active_idx == 1)
                assert session.history is not None          # _swap_history fired, same as a click
                pipe.send_text("\x04")                       # single session -> idle exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_lines_typed_before_the_first_switch_recall_after_home_and_back():
    # FIX7c: the boot-active slot's history is seeded BEFORE app.run_async
    # even starts -- a line typed before ANY switch must land in THAT
    # slot's own persistent history, not the Buffer's throwaway default
    # (which the OLD code left `buf.history` pointed at until the first
    # actual `_switch_to` call minted a brand-new, EMPTY history for the
    # slot -- silently losing everything typed before that first switch).
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="proj",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1                          # boots landed on the session, like repl does

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                assert session.history is not None       # seeded from the START, before any key
                seeded = session.history

                pipe.send_text("line one\r")               # typed BEFORE ever switching tabs
                await asyncio.sleep(0.05)

                pipe.send_text("\x14")                      # Ctrl-T -- jump to Home
                await _until(lambda: slots.active_idx == 0)
                pipe.send_text("\x1b1")                      # Alt+1 -- back to the session
                await _until(lambda: slots.active_idx == 1)

                assert session.history is seeded             # never replaced by a fresh, empty one
                assert "line one" in seeded.get_strings()    # survived the round trip

                pipe.send_text("\x04")                        # idle, single session -> exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_ctrl_w_with_empty_buffer_closes_active_session_and_notes_survivor():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        notes_a = []
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(note=notes_a.append), agent=None)
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.add(slot_b)
        slots.active_idx = 2                                # active = b, input empty

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("\x17")                       # Ctrl-W, empty buffer, active=b
                await _until(lambda: len(slots.slots) == 2)
                assert slots.active_idx == 1                 # landed on the neighbor (a)
                assert notes_a and "server-side" in notes_a[0] and "/new" in notes_a[0]
                pipe.send_text("\x04")                        # single session left -> idle exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_close_flow_points_history_at_the_survivor_slot():
    # FIX7d: closing a tab must repoint the shared input buffer's history at
    # the SURVIVOR (post-close active) slot's own persistent history --
    # a closed tab's history dies with it, so the buffer must never be left
    # pointing at it.
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.add(slot_b)
        slots.active_idx = 2                                # active = b, input empty
        assert slot_a.history is None                        # never touched yet

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("\x17")                        # Ctrl-W, empty buffer, active=b -> closes b
                await _until(lambda: len(slots.slots) == 2)
                assert slots.active_idx == 1                  # landed on the survivor (a)
                assert slot_a.history is not None              # FIX7d: seeded on the way in

                pipe.send_text("after close\r")                # typed into the buffer NOW
                await asyncio.sleep(0.05)
                assert "after close" in slot_a.history.get_strings()   # recorded into A's OWN history

                pipe.send_text("\x04")                          # single session left -> idle exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_ctrl_w_with_draft_text_falls_through_to_word_delete_not_close():
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.add(slot_b)
        slots.active_idx = 2

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("hello world")
                await asyncio.sleep(0.05)
                before = get_app().current_buffer.text
                assert before == "hello world"
                pipe.send_text("\x17")                        # Ctrl-W with draft text present
                await asyncio.sleep(0.05)
                after = get_app().current_buffer.text
                assert after != before and len(after) < len(before)   # normal word-delete ran
                assert len(slots.slots) == 3                  # nothing closed
                assert slots.active_idx == 2
                get_app().current_buffer.reset()
                slots.active_idx = 1                          # avoid closing idx 0 on exit
                pipe.send_text("\x04")
                await _until(lambda: len(slots.slots) == 2)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_ctrl_d_closes_active_session_when_others_remain():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.add(slot_b)
        slots.active_idx = 2                                  # active = b (session), idle

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("\x04")                        # closes b (session_count 2 > 1)
                await _until(lambda: len(slots.slots) == 2)
                assert slots.active_idx == 1
                pipe.send_text("\x04")                        # now exits (single session, idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_ctrl_d_on_home_is_a_noop_while_a_background_session_turn_is_alive():
    # FIX5: Ctrl-D must not exit right through a LIVE turn running in a
    # BACKGROUND session tab just because Home (or any other idle tab)
    # happens to be the one visible -- _eof now checks EVERY slot
    # (_slot_busy), not only the active one.
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        pane_a = tui.OutputPane(width=80)
        gate = asyncio.Event()
        busy = {"v": False}
        ran = []

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink_a = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                                 consent_pending=lambda: False, resolve_consent=lambda t: None)
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=pane_a, sink=sink_a, agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.active_idx = 1                                    # A active, about to start a turn

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])            # A is now busy

                pipe.send_text("\x14")                             # Ctrl-T -- jump to Home
                await _until(lambda: slots.active_idx == 0)

                pipe.send_text("\x04")                              # Ctrl-D on Home -- must NOT exit
                await asyncio.sleep(0.15)
                assert not task.done()                              # the dock is still running

                gate.set()                                          # A's turn completes naturally
                await _until(lambda: not busy["v"])
                await asyncio.sleep(0.05)

                pipe.send_text("\x04")                               # Ctrl-D again -- idle now, exits
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_tab_bar_close_click_closes_the_clicked_background_tab_not_the_active_one():
    # Task 7 hygiene fix: clicking a SPECIFIC tab's ✕ must close THAT tab,
    # even when a DIFFERENT tab is the one currently active -- "honest v1"
    # (Task 5) always closed whichever tab was active, ignoring the clicked
    # idx entirely.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.add(slot_b)
        slots.active_idx = 2                                  # active = b

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                bar = get_app().layout.container.children[0]
                frags = bar.content.text()
                # frags: [home, sep, a-body, a-pad, a-GLYPH, a-pad, sep,
                #         b-body, b-pad, b-GLYPH, b-pad, sep, +-pad, +, +-pad]
                close_a = frags[4]
                assert close_a[1] == "✕"
                ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                                button=MouseButton.LEFT, modifiers=frozenset())
                close_a[2](ev)                                 # click ✕ on the BACKGROUND tab a
                await asyncio.sleep(0.02)
                assert len(slots.slots) == 2                   # a is gone
                assert slots.slots[1] is slot_b                # b survives, untouched
                assert slots.active_idx == 1                   # still pointed at b (index shifted)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_tab_bar_close_click_closes_the_active_tab_when_that_is_the_one_clicked():
    # The other half of the fix: clicking the ✕ on the tab that IS active
    # still closes it (unchanged from "honest v1" in this one case).
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        notes_a = []
        slot_a = SessionSlot(kind="session", workspace=".", label="a",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(note=notes_a.append), agent=None)
        slot_b = SessionSlot(kind="session", workspace=".", label="b",
                             pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(slot_a)
        slots.add(slot_b)
        slots.active_idx = 2                                  # active = b

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                bar = get_app().layout.container.children[0]
                frags = bar.content.text()
                close_b = frags[9]
                assert close_b[1] == "✕"
                ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                                button=MouseButton.LEFT, modifiers=frozenset())
                close_b[2](ev)                                 # click ✕ on the ACTIVE tab b
                await asyncio.sleep(0.02)
                assert len(slots.slots) == 2                   # b is gone
                assert slots.active_idx == 1                   # landed on the neighbor (a)
                assert notes_a and "server-side" in notes_a[0]  # same close-note behavior as before
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── 0.3.25 Part D: busy-close confirm (click ✕ on a running tab) ───────────

def test_close_click_on_a_busy_tab_arms_instead_of_closing():
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        notes = []
        session = SessionSlot(kind="session", workspace=".", label="alpha",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(note=notes.append), agent=None)
        session.turn["task"] = _FakeTask()   # a live turn -- done() is always False
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                bar = get_app().layout.container.children[0]
                close_glyph = bar.content.text()[4]
                assert close_glyph[1] == "✕"
                ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                                button=MouseButton.LEFT, modifiers=frozenset())
                close_glyph[2](ev)
                await asyncio.sleep(0.02)
                assert len(slots.slots) == 2            # NOT closed
                assert session.close_armed is True
                assert any("busy" in n for n in notes)
                # rendered as the armed "✕?" glyph now
                armed_glyph = bar.content.text()[4]
                assert armed_glyph[1] == "✕?"
                session.turn["task"] = None             # let a real close through below
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_second_close_click_on_an_armed_busy_tab_actually_closes():
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="alpha",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        session.close_armed = True   # already armed by an earlier click
        session.turn["task"] = _FakeTask()
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                bar = get_app().layout.container.children[0]
                armed_glyph = bar.content.text()[4]
                assert armed_glyph[1] == "✕?"
                ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                                button=MouseButton.LEFT, modifiers=frozenset())
                armed_glyph[2](ev)
                await asyncio.sleep(0.02)
                assert len(slots.slots) == 1             # closed for real this time
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_switching_tabs_disarms_a_busy_close_confirm():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="alpha",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        session.close_armed = True
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("\x14")     # Ctrl-T -- jumps to Home, a genuine switch
                await asyncio.sleep(0.02)
                assert session.close_armed is False
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_any_keypress_disarms_a_busy_close_confirm():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="alpha",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        session.close_armed = True
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 1

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                pipe.send_text("x")        # an ordinary typed character
                await asyncio.sleep(0.02)
                assert session.close_armed is False
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── 0.3.25: the tab bar's "+" chip opens a new tab like a browser ──────────

def test_new_chip_click_fires_the_wired_on_new_callback():
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        slots = mk_slots(pane=tui.OutputPane(width=80), sink=_idle_sink())
        calls = []

        async def on_new():
            calls.append(1)

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None, on_new=on_new))
                await asyncio.sleep(0.05)
                bar = get_app().layout.container.children[0]
                new_glyph = bar.content.text()[-2]
                assert new_glyph[1] == "+"
                ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                                button=MouseButton.LEFT, modifiers=frozenset())
                new_glyph[2](ev)
                await asyncio.sleep(0.02)
                assert calls == [1]
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_new_chip_click_with_no_on_new_wired_is_a_harmless_noop_in_the_dock():
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        slots = mk_slots(pane=tui.OutputPane(width=80), sink=_idle_sink())

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))   # no on_new
                await asyncio.sleep(0.05)
                bar = get_app().layout.container.children[0]
                new_glyph = bar.content.text()[-2]
                ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                                button=MouseButton.LEFT, modifiers=frozenset())
                assert new_glyph[2](ev) is None   # consumed, never raises
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_key_processor_timeoutlen_tuned_down_below_default():
    # Registering ("escape", "<digit>") chords makes bare Escape a prefix of
    # a longer match, so the key-processor would otherwise wait its FULL
    # default timeoutlen (1.0s) before resolving a genuinely lone Escape --
    # this guards that the Task 5 mitigation (a shorter app.timeoutlen) is
    # actually in place on the constructed Application.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        slots = mk_slots(pane=tui.OutputPane(width=80), sink=_idle_sink())
        async def on_line(text, slot=None): ...
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)
                assert get_app().timeoutlen < 1.0
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_lone_escape_still_stops_the_turn_after_alt_digit_bindings_added():
    # The regression Task 5 explicitly calls out: adding ("escape","0".."9")
    # bindings must not silently BREAK the existing stop-turn Escape -- it
    # may resolve a little later (key-processor chord-timeout), but it must
    # still fire. Generous timeout budget covers app.timeoutlen (tuned down,
    # see above) plus prompt_toolkit's own unrelated vt100-level ttimeoutlen.
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        gate = asyncio.Event()
        busy = {"v": False}
        ran = []
        stopped = []

        async def on_line(text, slot=None):
            busy["v"] = True
            ran.append(text)
            try:
                await gate.wait()
            finally:
                busy["v"] = False

        async def stop_turn():
            stopped.append(1)

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        sink = SimpleNamespace(status=status, is_busy=lambda: busy["v"],
                               consent_pending=lambda: False, resolve_consent=lambda t: None)
        slots = mk_slots(pane=tui.OutputPane(width=80), sink=sink)

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None, stop_turn=stop_turn))
                await asyncio.sleep(0.05)
                pipe.send_text("go\r")
                await _until(lambda: ran == ["go"])
                pipe.send_text("\x1b")                       # LONE Escape -- nothing follows it
                await _until(lambda: stopped == [1], timeout=3.0)
                await _until(lambda: not busy["v"], timeout=3.0)
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_ui_hooks_filled_with_switch_and_close_routing_through_the_real_flow():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from webbee.slots import SessionSlot, SlotManager

    async def scenario():
        home = SessionSlot(kind="home", workspace=".", label="Home",
                           pane=tui.OutputPane(width=80), sink=None, agent=None)
        session = SessionSlot(kind="session", workspace=".", label="proj",
                              pane=tui.OutputPane(width=80), sink=_idle_sink(), agent=None)
        slots = SlotManager()
        slots.add(home)
        slots.add(session)
        slots.active_idx = 0
        ui_hooks = {}

        async def on_line(text, slot=None): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None, ui_hooks=ui_hooks))
                await asyncio.sleep(0.05)
                assert set(ui_hooks) == {"switch", "close", "start_turn_in", "start_attach_in"}
                assert ui_hooks["close"]() is False          # Home guarded -- no-op
                assert len(slots.slots) == 2

                ui_hooks["switch"](1)                        # same effect as a tab-bar click
                await asyncio.sleep(0.02)
                assert slots.active_idx == 1
                assert session.history is not None            # history swap happened too

                assert ui_hooks["close"]() is True
                await asyncio.sleep(0.02)
                assert len(slots.slots) == 1                  # only Home left

                pipe.send_text("\x04")                        # idle, no sessions -> exit
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())
