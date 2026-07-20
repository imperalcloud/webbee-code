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
                pane.scroll(-3)
                return None
            if et == MouseEventType.SCROLL_DOWN:
                pane.scroll(3)
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
                self._down = ev.position           # viewport point — click-vs-drag test only
                self._down_abs = (ev.position.y + pane._offset, ev.position.x)
                pane._sel = (self._down_abs, self._down_abs)  # zero-width start (no highlight yet)
                pane._invalidate()
                return None
            if et == MouseEventType.MOUSE_MOVE:
                if self._down_abs is None:
                    return NotImplemented
                y = ev.position.y
                if y >= pane._view_h - 1:
                    pane.scroll(3)
                    pane._edge_drag = 1
                elif y <= 0:
                    pane.scroll(-3)
                    pane._edge_drag = -1
                else:
                    pane._edge_drag = 0
                pane._sel = (self._down_abs, (ev.position.y + pane._offset, ev.position.x))
                pane._invalidate()                 # grow the highlight as you drag
                return None
            if et == MouseEventType.MOUSE_UP:
                down, self._down = self._down, None
                down_abs, self._down_abs = self._down_abs, None
                pane._edge_drag = 0
                if down is not None and (down.x, down.y) != (ev.position.x, ev.position.y):
                    pane._copy_selection(down_abs, (ev.position.y + pane._offset, ev.position.x))
                pane._sel = None
                pane._invalidate()                 # clear the highlight (colours restored)
                return None
            return NotImplemented

    return _SelectControl


def forward_mouse(pane, ev) -> bool:
    """W2 Task 8: prompt_toolkit has NO mouse capture — events route by
    pointer POSITION, not by who owns an in-progress drag — so today
    releasing (or moving) past the pane's Window while dragging just lands
    on whatever neighbor window sits under the pointer, and the pane never
    sees it: the highlight sticks forever and the copy never fires. Neighbor
    windows (queue/todo panels, toolbar) call this FIRST, before their own
    mouse handling.

    No drag armed (`pane.control._down_abs is None`) → False immediately,
    untouched — the caller falls through to its own handling. While armed,
    only MOUSE_MOVE/MOUSE_UP are treated specially (anything else — a stray
    SCROLL, say — is left to the neighbor too): the event is treated as if
    it had hit the pane's BOTTOM row (y clamped to `_view_h - 1`; x passed
    through unchanged), mirroring the edge-drag extension `edge_tick`
    already performs while parked at the viewport edge. MOUSE_MOVE extends
    `_sel` and arms `_edge_drag = 1` (unconditionally bottom — a forwarded
    move only happens below the pane, never above it). MOUSE_UP completes
    the copy exactly like the control's own MOUSE_UP, EXCEPT the click-vs-
    drag same-position check is skipped on purpose: a forwarded release only
    reaches here because the pointer already left the pane while the button
    was down, so it is by definition a drag, never a click. Either way,
    returns True (consumed)."""
    from prompt_toolkit.mouse_events import MouseEventType

    control = pane.control
    if control._down_abs is None:
        return False
    et = ev.event_type
    if et not in (MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP):
        return False
    row = pane._offset + pane._view_h - 1
    x = ev.position.x
    if et == MouseEventType.MOUSE_MOVE:
        pane._sel = (control._down_abs, (row, x))
        pane._edge_drag = 1
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
