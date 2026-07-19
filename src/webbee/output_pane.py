"""The full-screen dock's scrollable, colored output region. Rich renders the
full transcript into a StringIO as ANSI; the pane VIRTUALIZES — the
FormattedTextControl is fed ONLY the currently-visible slice of lines, so
every frame costs O(viewport), not O(session). That keeps huge sessions
lag-free and never truncates a long answer (the visible region is always
rendered in full; scroll to see more). Wheel / PageUp move `_offset`;
left-drag copies the covered text to the real local clipboard
(`webbee.clipboard`), OSC 52 only as a fallback. Split out of tui.py to keep
both files under the file-size ceiling. Grounded in prompt_toolkit 3.0.52
(verified in venv)."""


class OutputPane:
    def __init__(self, width: int = 100) -> None:
        import io

        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType, MouseButton
        from rich.console import Console

        self._io = io.StringIO()
        self.console = Console(file=self._io, force_terminal=True,
                               color_system="truecolor", width=width, highlight=False)
        self._ANSI = ANSI
        self._lines_cache = (0, [""])      # (write-pos, split-lines) — memoize the split
        self._offset = 0                   # index of the top visible line
        self._view_h = 20                  # viewport height (updated from render_info)
        self._follow = True                # stick to the tail unless the user scrolled up
        self._sel = None                   # (abs_start, abs_end) during a drag → live highlight
        self._plain_cache = (0, [""])      # (write-pos, ANSI-stripped lines) for select/highlight
        self.copy_flash = ""               # transient toolbar note after a copy
        self._flash_until = 0.0
        pane = self

        # Content fed to the control is only the visible slice, so the mouse row
        # is a VIEWPORT row (0..view_h-1) — add `_offset` for the absolute line.
        # Wheel scroll adjusts `_offset` directly (the control shows no more than
        # a viewport, so there is nothing for the Window itself to scroll).
        class _SelectControl(FormattedTextControl):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._down = None

            def mouse_handler(self, ev):
                et = ev.event_type
                if et == MouseEventType.SCROLL_UP:
                    pane.scroll(-3)
                    return None
                if et == MouseEventType.SCROLL_DOWN:
                    pane.scroll(3)
                    return None
                if et == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.LEFT:
                    self._down = ev.position
                    a = (ev.position.y + pane._offset, ev.position.x)
                    pane._sel = (a, a)                 # zero-width start (no highlight yet)
                    pane._invalidate()
                    return None
                if et == MouseEventType.MOUSE_MOVE:
                    if self._down is None:
                        return NotImplemented
                    pane._sel = ((self._down.y + pane._offset, self._down.x),
                                 (ev.position.y + pane._offset, ev.position.x))
                    pane._invalidate()                 # grow the highlight as you drag
                    return None
                if et == MouseEventType.MOUSE_UP:
                    down, self._down = self._down, None
                    if down is not None and (down.x, down.y) != (ev.position.x, ev.position.y):
                        pane._copy_selection(down, ev.position)  # real drag, not a click
                    pane._sel = None
                    pane._invalidate()                 # clear the highlight (colours restored)
                    return None
                return NotImplemented

        self.control = _SelectControl(text=self._formatted, focusable=False, show_cursor=False)
        self.window = Window(content=self.control, wrap_lines=False, always_hide_cursor=True)

    # ---- virtualized render ---------------------------------------------
    def _all_lines(self):
        # Key the cache on the stream WRITE POSITION (O(1)), not a full-buffer
        # getvalue()+string compare (O(session)) — that ran on EVERY redraw
        # (keystroke / ticker / scroll) and made big sessions lag. Beyond that,
        # only the DELTA since the cached position is read and split — the
        # cached list is extended IN PLACE, so a print costs O(new output),
        # never a full-buffer re-split (that made long busy streams
        # quadratic). Boundary safety: the cache only ever advances at print
        # boundaries (Console.print completes before notify() runs), so an
        # SGR escape can never split across a delta read.
        pos = self._io.tell()
        cpos, lines = self._lines_cache
        if cpos == pos:
            return lines
        if isinstance(cpos, int) and 0 <= cpos < pos:
            self._io.seek(cpos)
            delta = self._io.read()          # leaves position at EOF (== pos)
            parts = delta.split("\n")
            lines[-1] += parts[0]
            lines.extend(parts[1:])
        else:
            lines = self._io.getvalue().split("\n")
        self._lines_cache = (pos, lines)
        return lines

    def _plain_lines(self):
        import re
        pos = self._io.tell()
        cpos, lines = self._plain_cache
        if cpos == pos:
            return lines
        if isinstance(cpos, int) and 0 <= cpos < pos:
            self._io.seek(cpos)
            delta = re.sub(r"\x1b\[[0-9;]*m", "", self._io.read())
            parts = delta.split("\n")
            lines[-1] += parts[0]
            lines.extend(parts[1:])
        else:
            lines = re.sub(r"\x1b\[[0-9;]*m", "", self._io.getvalue()).split("\n")
        self._plain_cache = (pos, lines)
        return lines

    def _norm_sel(self):
        (a, b) = self._sel
        return (a, b) if a <= b else (b, a)

    def _formatted(self):
        ri = self.window.render_info
        if ri is not None and ri.window_height:
            self._view_h = ri.window_height
        lines = self._all_lines()
        h = max(1, self._view_h)
        off = max(0, min(self._offset, max(0, len(lines) - h)))
        self._offset = off
        visible = lines[off:off + h]
        if self._sel is None:
            return self._ANSI("\n".join(visible))
        # A drag is in progress — overlay reverse-video on the selected columns
        # (selected lines render plain+reversed; colours return when it clears).
        (y1, x1), (y2, x2) = self._norm_sel()
        plain = self._plain_lines()
        out = []
        for vi, aln in enumerate(range(off, off + len(visible))):
            if y1 <= aln <= y2 and aln < len(plain):
                ln = plain[aln]
                a = max(0, min(x1 if aln == y1 else 0, len(ln)))
                b = max(-1, min(x2 if aln == y2 else len(ln) - 1, len(ln) - 1))
                out.append(ln[:a] + "\x1b[7m" + ln[a:b + 1] + "\x1b[0m" + ln[b + 1:]
                           if b >= a else ln)
            else:
                out.append(visible[vi])
        return self._ANSI("\n".join(out))

    def scroll(self, delta: int) -> None:
        lines = self._all_lines()
        max_off = max(0, len(lines) - max(1, self._view_h))
        self._offset = max(0, min(self._offset + delta, max_off))
        self._follow = self._offset >= max_off   # re-arm tail-follow once back at bottom
        self._invalidate()

    def notify(self) -> None:
        """After each sink print: follow the tail unless the user scrolled up."""
        self._trim()
        lines = self._all_lines()
        max_off = max(0, len(lines) - max(1, self._view_h))
        self._offset = max_off if self._follow else min(self._offset, max_off)
        self._invalidate()

    def _invalidate(self) -> None:
        try:
            from prompt_toolkit.application import get_app_or_none
            app = get_app_or_none()
            if app is not None:
                app.invalidate()
        except Exception:
            pass

    def _trim(self, max_lines: int = 20000, keep: int = 15000) -> None:
        # Hysteresis: only trim once past max_lines, and cut down to `keep`
        # (not max_lines) — trimming is amortized over 5000 lines of headroom
        # instead of firing on every single print once the ceiling is hit.
        import io
        lines = self._all_lines()
        if len(lines) <= max_lines:
            return
        dropped = len(lines) - keep
        s = "\n".join(lines[-keep:])
        self._io = io.StringIO()
        self._io.write(s)
        self.console.file = self._io
        self._lines_cache = (0, [""])
        self._plain_cache = (0, [""])
        # A scrolled-up reader keeps looking at the SAME content lines — the
        # dropped count is subtracted from `_offset` so the viewport doesn't
        # silently jump when the buffer is rewritten underneath it.
        self._offset = max(0, self._offset - dropped)

    def dump(self) -> str:
        """The full session transcript (ANSI). Printed to real stdout on exit so
        the conversation survives leaving the alternate screen."""
        return self._io.getvalue()

    # ---- copy-on-select --------------------------------------------------
    def _selected_text(self, start, end) -> str:
        """Plain text (ANSI stripped) covered by a drag. start/end .y are
        VIEWPORT rows → add `_offset` for absolute content lines. wrap_lines is
        False so a content line maps 1:1 to a `\\n`-split line, col = char index."""
        lines = self._plain_lines()
        p1 = (start.y + self._offset, start.x)
        p2 = (end.y + self._offset, end.x)
        if p1 > p2:
            p1, p2 = p2, p1
        (y1, x1), (y2, x2) = p1, p2
        n = len(lines)
        if not (0 <= y1 < n):
            return ""
        y2 = min(y2, n - 1)
        if y1 == y2:
            return lines[y1][x1:x2 + 1]
        out = [lines[y1][x1:]]
        out.extend(lines[y1 + 1:y2])
        out.append(lines[y2][:x2 + 1])
        return "\n".join(out)

    def _copy_selection(self, start, end) -> None:
        import time as _t

        from webbee.clipboard import copy_to_clipboard
        text = self._selected_text(start, end)
        if not text.strip():
            return
        self.copy_flash = copy_to_clipboard(text)  # local clipboard first, OSC 52 fallback
        self._flash_until = _t.monotonic() + 1.8
        self.notify()

    def flash(self) -> str:
        """The transient 'copied' note, while still fresh (else empty)."""
        import time as _t
        return self.copy_flash if _t.monotonic() < self._flash_until else ""
