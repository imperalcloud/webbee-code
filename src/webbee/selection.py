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
    line.
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
                self._down = ev.position           # viewport point — click-vs-drag test only
                self._down_abs = (ev.position.y + pane._offset, ev.position.x)
                pane._sel = (self._down_abs, self._down_abs)  # zero-width start (no highlight yet)
                pane._invalidate()
                return None
            if et == MouseEventType.MOUSE_MOVE:
                if self._down_abs is None:
                    return NotImplemented
                pane._sel = (self._down_abs, (ev.position.y + pane._offset, ev.position.x))
                pane._invalidate()                 # grow the highlight as you drag
                return None
            if et == MouseEventType.MOUSE_UP:
                down, self._down = self._down, None
                down_abs, self._down_abs = self._down_abs, None
                if down is not None and (down.x, down.y) != (ev.position.x, ev.position.y):
                    pane._copy_selection(down_abs, (ev.position.y + pane._offset, ev.position.x))
                pane._sel = None
                pane._invalidate()                 # clear the highlight (colours restored)
                return None
            return NotImplemented

    return _SelectControl
