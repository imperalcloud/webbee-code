"""Extracted from OutputPane.__init__ (W2 front-3a: file-ceiling headroom, no
behavior change at extraction time). `make_select_control` is a FACTORY, not a
plain module-level class, because the control's mouse_handler closes over a
specific `pane` instance — each OutputPane needs its own class closed over
its own pane, not a class shared (and thus cross-wired) across panes. The
prompt_toolkit enums/base class are passed in rather than imported here so
this module stays prompt_toolkit-import-free until actually wired up by
output_pane.py, matching the rest of the codebase's late-import convention."""
from __future__ import annotations


def make_select_control(pane, FormattedTextControl, MouseEventType, MouseButton):
    """Build the `_SelectControl` class closed over `pane` (+ the
    prompt_toolkit mouse enums / base control class it needs).

    Content fed to the control is only the visible slice, so the mouse row
    is a VIEWPORT row (0..view_h-1) — add `pane._offset` for the absolute
    line. While dragging, MOUSE_MOVE at a viewport edge (top or bottom row)
    also nudges the scroll and arms `pane._edge_drag` (+1 bottom, -1 top, 0
    elsewhere) so the dock's ticker (`OutputPane.edge_tick`) can keep
    scrolling — and keep growing the selection — while the pointer sits
    still at the edge (no MOUSE_MOVE arrives when the mouse is stationary).
    """

    class _SelectControl(FormattedTextControl):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._down = None
            self._down_abs = None   # (line, col) anchor, frozen at MOUSE_DOWN — never re-derived

        def mouse_handler(self, ev):
            et = ev.event_type
            if et == MouseEventType.SCROLL_UP:
                # W2 final-review Fix 3b: the user's wheel wins over a runaway
                # edge auto-scroll — disarm edge_drag, but the armed selection
                # itself (`_sel`/`_down_abs`) stays exactly as-is.
                pane._edge_drag = 0
                pane.scroll(-3)
                return None
            if et == MouseEventType.SCROLL_DOWN:
                pane._edge_drag = 0
                pane.scroll(3)
                return None
            if et == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.MIDDLE:
                # X11/Linux convention: middle-click pastes the PRIMARY
                # selection (last-selected text), independent of Ctrl+V's
                # CLIPBOARD read. Fires the SAME on_paste upload door via a
                # pane-level hook (tui.py wires `pane.on_middle_paste`) --
                # one paste implementation, two entry points. A sync mouse
                # handler can't await, so the hook itself schedules its own
                # background task; a pane with no hook wired (tests, no
                # dock) is a harmless no-op.
                hook = getattr(pane, "on_middle_paste", None)
                if hook is not None:
                    hook()
                return None
            if et == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.RIGHT:
                # webbee-code-mouse-right-click-paste-v1: the OTHER common
                # terminal convention (PuTTY / Windows Terminal / many Linux
                # emulators) -- right-click pastes the regular OS CLIPBOARD
                # (same source Ctrl+V reads, text OR image), independent of
                # middle-click's PRIMARY-selection read. Same one-hook,
                # sync-dispatch discipline as MIDDLE above; a pane with no
                # hook wired (tests, no dock) is a harmless no-op.
                hook = getattr(pane, "on_right_paste", None)
                if hook is not None:
                    hook()
                return None
            if et == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.LEFT:
                if self._down_abs is not None:
                    # A previous drag never got its MOUSE_UP (prompt_toolkit has
                    # no mouse capture — a release past a neighbor window used to
                    # just vanish, the W1-recon stuck-highlight case). Clear the
                    # stale edge-scroll flag before arming the new drag so
                    # edge_tick doesn't act on a leftover edge from the drag that
                    # never closed; `_down`/`_down_abs`/`_sel` are overwritten
                    # below regardless, so they need no separate reset.
                    pane._edge_drag = 0
                self._down = ev.position           # viewport point (debug/reflow-abort only now)
                self._down_abs = (ev.position.y + pane._offset, ev.position.x)
                pane._sel = (self._down_abs, self._down_abs)  # zero-width start (no highlight yet)
                pane._edge_ticks = 0                # fresh drag — the runaway-scroll clock resets
                pane._invalidate()
                return None
            if et == MouseEventType.MOUSE_MOVE:
                if self._down_abs is None:
                    return NotImplemented
                pane._edge_ticks = 0                # a fresh MOUSE_MOVE means the pointer isn't parked
                y = ev.position.y
                # webbee-code-selection-scroll-jitter-v1: a REAL terminal fires
                # MOUSE_MOVE far more often than once (every pixel of hand
                # tremor while parked at the edge trying to select further) --
                # scrolling immediately on EVERY one of those, on top of
                # edge_tick() ALREADY scrolling the same pane every 0.25s once
                # armed, stacked two independent scroll sources racing each
                # other and made the viewport visibly jump/jitter instead of
                # scrolling smoothly (Valentin, live 2026-07-31: "окно бегает
                # туда сюда"). The immediate nudge now fires ONLY on the edge
                # transition (armed 0 -> 1/-1) for snappy first-touch
                # feedback; every following MOUSE_MOVE that's STILL at the
                # same edge just re-affirms the arm (resets _edge_ticks above,
                # so the runaway guard doesn't fire) and leaves the ONGOING
                # scroll entirely to edge_tick()'s single steady cadence --
                # exactly the contract forward_mouse() already uses for the
                # neighbor panels.
                if y >= pane._view_h - 1:
                    if pane._edge_drag != 1:
                        pane.scroll(3)
                    pane._edge_drag = 1
                elif y <= 0:
                    if pane._edge_drag != -1:
                        pane.scroll(-3)
                    pane._edge_drag = -1
                else:
                    pane._edge_drag = 0
                pane._sel = (self._down_abs, (ev.position.y + pane._offset, ev.position.x))
                pane._invalidate()                 # grow the highlight as you drag
                return None
            if et == MouseEventType.MOUSE_UP:
                self._down = None
                down_abs, self._down_abs = self._down_abs, None
                pane._edge_drag = 0
                # W2 final-review Fix 4: click-vs-drag compares ABSOLUTE
                # endpoints, not viewport points — an edge auto-scroll during
                # the drag (Fix 3b's own MOUSE_MOVE branch) can land the
                # release on the SAME viewport cell the press used while the
                # content underneath has moved; the old viewport-only compare
                # missed exactly that case and silently dropped the copy.
                up_abs = (ev.position.y + pane._offset, ev.position.x)
                # webbee-code-click-jitter-tolerance-v1: a REAL mouse/trackpad
                # almost never releases at the EXACT pixel it pressed at --
                # even a "plain click, no drag intended" click commonly drifts
                # 1 cell in some direction (hand tremor, trackpad noise). The
                # old exact-equality compare meant on real hardware (any OS,
                # any terminal) down_abs != up_abs was ALWAYS true, so
                # click-to-expand (on_line_click) NEVER fired outside a
                # scripted test with perfect coordinates -- every real click
                # was silently treated as a (near-empty) drag-copy instead
                # (Valentin, live 2026-07-31: "никаких раскрытий вкладок я
                # вообще не вижу... ни на маке ни на линуксе"). A small
                # Chebyshev tolerance (<=1 cell either axis) still treats any
                # REAL drag (multiple cells/rows) as a copy, exactly as
                # before -- only genuine within-jitter releases now count as
                # a click.
                jitter = (abs(up_abs[0] - down_abs[0]) <= 1
                          and abs(up_abs[1] - down_abs[1]) <= 1) if down_abs is not None else False
                if down_abs is not None and down_abs != up_abs and not jitter:
                    pane._copy_selection(down_abs, up_abs)
                elif down_abs is not None:
                    # webbee-code-click-to-expand-v1: a PLAIN click (press and
                    # release at the SAME absolute cell -- no drag, nothing to
                    # copy) on a transcript line asks "what happened here?" --
                    # same one-hook/getattr-guarded shape as on_middle_paste/
                    # on_right_paste above, so a pane with no hook wired
                    # (tests, no dock) stays a harmless no-op.
                    hook = getattr(pane, "on_line_click", None)
                    if hook is not None:
                        hook(down_abs[0])
                pane._sel = None
                pane._invalidate()                 # clear the highlight (colours restored)
                return None
            return NotImplemented

    return _SelectControl


def forward_mouse(pane, ev, clamp: str = "bottom") -> bool:
    """W2 Task 8: prompt_toolkit has NO mouse capture — events route by
    pointer POSITION, not by who owns an in-progress drag — so today
    releasing (or moving) past the pane's Window while dragging just lands
    on whatever neighbor window sits under the pointer, and the pane never
    sees it: the highlight sticks forever and the copy never fires. Neighbor
    windows (queue/todo panels, toolbar, and — FIX6 — the tab bar) call this
    FIRST, before their own mouse handling.

    No drag armed (`pane.control._down_abs is None`) → False immediately,
    untouched — the caller falls through to its own handling. While armed, a
    MOUSE_DOWN (W2 final-review Fix 3a) means the matching MOUSE_UP was lost
    the same way the pane's OWN MOUSE_DOWN hygiene handles it — every stale
    drag field is cleared and this returns False so the neighbor's click
    proceeds untouched (no phantom copy, no swallowed pull/toggle). Otherwise
    only MOUSE_MOVE/MOUSE_UP are treated specially (anything else — a stray
    SCROLL, say — is left to the neighbor too): the event is treated as if
    it had hit the pane's edge given by `clamp` — "bottom" (default; the
    queue/todo panels and toolbar, all BELOW the pane: y clamped to
    `_view_h - 1`) or "top" (FIX6; the tab bar, ABOVE the pane: y clamped to
    row 0 — the pane's OWN top row, `pane._offset`) — x passed through
    unchanged either way, mirroring the edge-drag extension `edge_tick`
    already performs while parked at a viewport edge. MOUSE_MOVE extends
    `_sel` and arms `_edge_drag` (1 for "bottom", -1 for "top" — matching
    the SAME sign `_SelectControl`'s own MOUSE_MOVE uses for a real
    top/bottom-edge drag), and resets `_edge_ticks` — a forwarded move is
    still fresh motion, just past the pane's own edge. MOUSE_UP completes
    the copy exactly like the control's own MOUSE_UP, EXCEPT the
    click-vs-drag same-position check is skipped on purpose: a forwarded
    release only reaches here because the pointer already left the pane
    while the button was down, so it is by definition a drag, never a
    click. Either way, returns True (consumed)."""
    from prompt_toolkit.mouse_events import MouseEventType

    control = pane.control
    if control._down_abs is None:
        return False
    et = ev.event_type
    if et == MouseEventType.MOUSE_DOWN:
        control._down = None
        control._down_abs = None
        pane._sel = None
        pane._edge_drag = 0
        pane._invalidate()
        return False
    if et not in (MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP):
        return False
    is_top = clamp == "top"
    row = pane._offset if is_top else pane._offset + pane._view_h - 1
    x = ev.position.x
    if et == MouseEventType.MOUSE_MOVE:
        pane._sel = (control._down_abs, (row, x))
        pane._edge_drag = -1 if is_top else 1
        pane._edge_ticks = 0
        pane._invalidate()
        return True
    # MOUSE_UP
    down_abs, control._down_abs = control._down_abs, None
    control._down = None
    pane._edge_drag = 0
    pane._copy_selection(down_abs, (row, x))
    pane._sel = None
    pane._invalidate()
    return True
