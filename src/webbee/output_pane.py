"""The full-screen dock's scrollable, colored output region. Rich renders the
full transcript into a StringIO as ANSI; the pane VIRTUALIZES — the
FormattedTextControl is fed ONLY the currently-visible slice of lines, so
every frame costs O(viewport), not O(session). Wheel / PageUp move `_offset`;
left-drag copies to the local clipboard (`webbee.clipboard`), OSC 52 fallback."""

_MAX_RECORDS = 4000     # RecordingConsole ring bound (W2 front-2: replay material for reflow)


class OutputPane:
    def __init__(self, width: int = 100) -> None:
        import io
        from collections import deque

        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType, MouseButton
        from rich.console import Console

        self._io = io.StringIO()
        pane = self
        pane_records: deque = deque(maxlen=_MAX_RECORDS)

        class _RecordingConsole(Console):
            """Captures every printed renderable so a width change can REPLAY
            the transcript (old pane kept only baked ANSI, which can't
            re-wrap). Bounded ring: past 4000 records, only the tail replays."""

            def print(self, *objects, **kw):        # noqa: A003
                if getattr(pane, "_replaying", False):  # replay latch: never re-record
                    return super().print(*objects, **kw)
                evicting = len(pane_records) == pane_records.maxlen
                pane_records.append((objects, kw))
                pane._all_lines()                    # sync the W1 cache to the pre-write pos
                pos = pane._io.tell()
                result = super().print(*objects, **kw)
                pane._io.seek(pos)
                delta = pane._io.read()              # leaves position at EOF again
                if evicting and pane._record_lines:  # stay in lockstep with the deque's own drop
                    del pane._record_lines[0]
                pane._record_lines.append(delta.count("\n"))
                return result

            def clear(self, *a, **kw):
                pane_records.clear()
                pane._reset_buffer()                # StringIO + caches reset (Task 3 builds on it)

        self._records = pane_records
        self._record_lines: list = []      # per-record NEW-line count, prefix-summable (reflow.py)
        self._replaying = False            # True while a width-reflow replay is in flight
        self.console = _RecordingConsole(file=self._io, force_terminal=True,
                                         color_system="truecolor", width=width,
                                         highlight=False)
        self._ANSI = ANSI
        self._lines_cache = (0, [""])      # (write-pos, split-lines) — memoize the split
        self._offset = 0                   # index of the top visible line
        self._view_h = 20                  # viewport height (updated from render_info)
        self._follow = True                # stick to the tail unless the user scrolled up
        self._sel = None                   # (abs_start, abs_end) during a drag → live highlight
        self._plain_cache = (0, [""])      # (write-pos, ANSI-stripped lines) for select/highlight
        self.copy_flash = ""               # transient toolbar note after a copy
        self._flash_until = 0.0

        # The mouse-selection control lives in selection.py (file-ceiling
        # headroom) — a factory closed over this pane, not a shared class.
        from webbee.selection import make_select_control
        _SelectControl = make_select_control(pane, FormattedTextControl, MouseEventType, MouseButton)
        self.control = _SelectControl(text=self._formatted, focusable=False, show_cursor=False)
        self.window = Window(content=self.control, wrap_lines=False, always_hide_cursor=True)

    # ---- virtualized render ---------------------------------------------
    def _all_lines(self):
        # Cache keyed on the stream WRITE POSITION (O(1)); a hit returns the
        # list as-is, a miss reads only the DELTA and extends it IN PLACE —
        # never a full getvalue()+re-split (that was O(session) per print).
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
        # A drag is in progress — overlay reverse-video on the selected columns.
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

    def _record_at_line(self, line: int) -> int:
        """Record owning absolute content line `line` (reflow.py prefix sum)."""
        from webbee.reflow import record_at_line
        return record_at_line(self._record_lines, line)

    def reflow(self, new_width: int) -> None:
        """Replay the retained ring at a new width (true reflow — old ANSI
        can't re-wrap), anchored by RECORD not line index: a re-wrap changes
        how many lines a record spans, so only the record stays stable."""
        from rich.console import Console
        from webbee.reflow import anchor_offset
        if new_width == self.console.width or new_width < 10:
            return
        follow = self._follow
        top_record = 0 if follow else self._record_at_line(self._offset)
        self._sel = None                     # a mid-drag resize aborts the drag honestly
        self.control._down = None
        self.control._down_abs = None
        self.console.width = new_width
        self._reset_buffer()
        spans: list = []
        self._replaying = True
        try:
            for objects, kw in list(self._records):
                before = len(self._all_lines())
                Console.print(self.console, *objects, **kw)   # bypass the recording override
                spans.append(len(self._all_lines()) - before)
        finally:
            self._replaying = False
        self._record_lines = spans
        max_off = max(0, len(self._all_lines()) - max(1, self._view_h))
        self._offset = anchor_offset(spans, top_record, max_off, follow)
        self._follow = follow and self._offset >= max_off
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
        # Hysteresis: only trim past max_lines, cut down to `keep` (amortized).
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
        self._offset = max(0, self._offset - dropped)   # viewport anchored to the same content
        from webbee.reflow import records_to_drop        # keep the ring in step
        n_drop = records_to_drop(self._record_lines, dropped)
        for _ in range(n_drop):
            self._records.popleft()
        del self._record_lines[:n_drop]

    def _reset_buffer(self) -> None:
        """`/clear` and `reflow()` both wipe the transcript to empty — the
        pane owns the alt screen, so an emptied buffer IS the cleared state.
        `reflow()` rebuilds `_record_lines` right after calling this."""
        import io
        self._io = io.StringIO()
        self.console.file = self._io
        self._lines_cache = (0, [""])
        self._plain_cache = (0, [""])
        self._record_lines = []
        self._offset = 0

    def dump(self) -> str:
        """Full session transcript (ANSI), printed to real stdout on exit."""
        return self._io.getvalue()

    # ---- copy-on-select --------------------------------------------------
    def _selected_text(self, start, end) -> str:
        """Plain text (ANSI stripped) covered by a drag. start/end are
        ABSOLUTE (line, col) pairs already resolved by the caller — no offset conversion happens here."""
        lines = self._plain_lines()
        p1, p2 = (start, end) if start <= end else (end, start)
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

    def _copy_selection(self, start_abs, end_abs) -> None:
        # start_abs/end_abs: ABSOLUTE (line, col) pairs — see _selected_text.
        import time as _t
        from webbee.clipboard import copy_to_clipboard
        text = self._selected_text(start_abs, end_abs)
        if not text.strip():
            return
        self.copy_flash = copy_to_clipboard(text)  # local clipboard first, OSC 52 fallback
        self._flash_until = _t.monotonic() + 1.8
        self.notify()

    def flash(self) -> str:
        """The transient 'copied' note, while still fresh (else empty)."""
        import time as _t
        return self.copy_flash if _t.monotonic() < self._flash_until else ""
