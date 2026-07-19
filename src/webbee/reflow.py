"""Pure prefix-sum math behind OutputPane.reflow (W2 front-2: true width
reflow). A terminal-width change re-wraps every retained record, which
shuffles absolute line indices around — the only stable anchor across a
re-wrap is which RECORD produced the top-visible line, not the line index
itself. Kept as free functions (no OutputPane/Rich/prompt_toolkit import) so
the anchoring math is unit-testable without constructing a real pane."""
from __future__ import annotations


def record_at_line(record_lines: list[int], line: int) -> int:
    """Record index whose lines cover absolute content line `line`, via a
    prefix sum over per-record line counts. Falls back to the last record
    (or 0 for an empty ring) once `line` runs past every recorded span —
    covers both a stale/out-of-range offset and the trailing placeholder
    line that follows the final recorded newline."""
    acc = 0
    for idx, n in enumerate(record_lines):
        acc += n
        if line < acc:
            return idx
    return max(0, len(record_lines) - 1)


def anchor_offset(record_lines: list[int], top_record: int, max_off: int, follow: bool) -> int:
    """New `_offset` after a reflow. Tail-follow snaps straight to `max_off`
    (line indices are meaningless post-rewrap, so there's nothing to anchor
    to). Otherwise the anchor is the NEW start line of `top_record` — the sum
    of every earlier record's span — clamped so it never overruns the
    freshly re-wrapped content."""
    if follow:
        return max_off
    return min(sum(record_lines[:top_record]), max_off)


def records_to_drop(record_lines: list[int], dropped: int) -> int:
    """How many records from the front have ALL their lines inside the first
    `dropped` lines of a trim — the boundary record (partial overlap) keeps
    its full span rather than being split."""
    acc = 0
    n_drop = 0
    for n in record_lines:
        if acc + n > dropped:
            break
        acc += n
        n_drop += 1
    return n_drop
