"""The LIVE pending-queue panel pinned between the output pane and the input
box (0.3.13 — replaces the static `⋯ queued:` scrollback echoes, which
scrolled away, duplicated and never updated). The builders here are PURE
(unit-tested without an Application); tui.run_session mounts them in a
ConditionalContainer that occupies ZERO rows while the queue is empty, so
the empty-queue dock is pixel-identical to before. Item rows carry a
per-fragment mouse handler (prompt_toolkit 3.0.52 3-tuple fragments,
verified in venv) firing on MOUSE_UP only — a PASSIVE consumer of the
clicks that already flow under the ?1000/?1002 button-event tracking; it
enables nothing at the terminal level, so the 0.3.3 mouse-flood fix
(?1003 any-event stays off, tui.configure_mouse_modes) is untouched.
Split out of tui.py to keep both files under the file-size ceiling."""

QP_MAX_ITEMS = 5   # newest items shown; deeper queues add one `… +K more` row


def one_line(text: str, width: int) -> str:
    """PURE. Collapse whitespace/newlines so a queued item costs EXACTLY one
    panel row, truncating with `…` when it would overflow `width` columns
    (width<=0 = no truncation — headless/unknown terminal)."""
    t = " ".join((text or "").split())
    if width > 0 and len(t) > width:
        t = t[:max(1, width - 1)] + "…"
    return t


def queue_height(pending, remote=None, collapsed=False, max_items=QP_MAX_ITEMS) -> int:
    """PURE. Rows the panel needs: 1 header + one per SHOWN item + one
    `… +K more` row when a queue is deeper than `max_items` (each of the
    two sections — remote rows and local rows — caps independently). 0 when
    both are empty (the ConditionalContainer hides the panel then anyway).
    `collapsed=True` (Task 11 click-to-collapse) always costs exactly 1 row
    when there's data — screen space back on demand. `max_items` defaults to
    QP_MAX_ITEMS but the dock overrides it with a LIVE, terminal-proportional
    cap (sizing.panel_cap) so a huge screen shows more and a tiny one shows
    less — the toolbar's `⋯N queued` segment stays the truth-teller for the
    full depth either way."""
    n = len(pending)
    r = len(remote or ())
    if not n and not r:
        return 0
    if collapsed:
        return 1
    rows = 1
    if r:
        rows += min(r, max_items) + (1 if r > max_items else 0)
    if n:
        rows += min(n, max_items) + (1 if n > max_items else 0)
    return rows


def pull_item(pending, buf, index: int):
    """The ONE pull-to-edit implementation (serves BOTH the ↑ key — newest,
    index len(pending)-1 — and a panel-row click — that row's index): move
    pending[index] OUT of the queue and into the input buffer for editing,
    cursor at the end. Guards, identical on both paths: a buffer with ANY
    text is never clobbered, and a stale index (the queue drained between
    render and click) is ignored. Returns the removed item (truthy) or None
    — callers truthy-check, so a QueuedLine's carried steer_iid rides back
    out with it (tui._rewrap_pulled reuses it when the line is resubmitted
    unchanged)."""
    if buf.text or not (0 <= index < len(pending)):
        return None
    item = pending[index]
    del pending[index]
    buf.text = str(item)
    buf.cursor_position = len(str(item))
    return item


def _item_handler(pull, index: int, forward=None):
    """One row's mouse handler: MOUSE_UP (a click, not a drag/press) pulls
    THAT queued item into the input via `pull(index)`; every other event
    falls through (NotImplemented) so wheel scroll etc. keep today's
    behavior. Mirrors OutputPane._SelectControl's event discipline.

    `forward` (W2 Task 8, `OutputPane.forward_mouse`) gets FIRST refusal
    when given: prompt_toolkit routes mouse events by pointer position, so a
    drag armed on the pane above can end up releasing on this row — a real
    click and a forwarded drag-release are indistinguishable to THIS row
    except by asking the pane. Consumed (a drag was armed) ⇒ stop here,
    pull is NOT called — the row must not also register as a click."""
    def _h(mouse_event):
        from prompt_toolkit.mouse_events import MouseEventType
        if forward is not None and forward(mouse_event):
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            pull(index)
            return None
        return NotImplemented
    return _h


def _toggle_handler(toggle, forward=None):
    """Header mouse handler (Task 11 click-to-collapse): MOUSE_UP toggles
    collapse; everything else falls through (NotImplemented) so wheel scroll
    keeps working — the exact event discipline of _item_handler. `forward`
    (W2 Task 8) is the same first-refusal seam as _item_handler's."""
    def _h(mouse_event):
        from prompt_toolkit.mouse_events import MouseEventType
        if forward is not None and forward(mouse_event):
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            toggle()
            return None
        return NotImplemented
    return _h


def queue_fragments(pending, pull=None, width: int = 0, remote=None,
                     collapsed=False, toggle=None, max_items=QP_MAX_ITEMS,
                     forward=None):
    """PURE builder: the panel as prompt_toolkit formatted text, re-invoked
    every redraw (same live mechanics as the toolbar) so every queue
    add/edit/drain shows at once. Layout, top→bottom = drain order (FIFO —
    the TOP row runs next; the BOTTOM row is the newest, the one ↑ pulls,
    sitting right above the input):

        ⋯ queued (N) · ↑ edit last · click to edit
        [telegram] remote item   ← remote rows ABOVE local (qp.remote)
        … +K more            ← only when N > max_items (the OLDEST hide;
                                defaults to QP_MAX_ITEMS, overridden by the
                                dock with a live sizing.panel_cap(rows))
        older item           ← muted (qp.item)
        newest item          ← accent (qp.last)

    `remote` (full-queue-layer K1) = cross-surface items already queued in
    the RUNNING kernel session ([{origin, text, iid}]); the kernel drains
    its own queue first, mid-run, while local type-ahead only runs after the
    whole turn returns — so remote rows render ABOVE local and top→bottom
    stays drain order. They are DISPLAY-ONLY: tagged `[origin]`, plain
    2-tuple fragments (never a mouse handler) and never part of the pull
    index space — you can't pull a kernel-queued item into the local input.
    The header counts both; the `↑ edit last` hint shows only when there is
    a local (pullable) item. When `pull` is given each LOCAL item row is a
    3-tuple fragment carrying a mouse handler that pulls exactly that item
    (see _item_handler). Both queues empty → [] (the panel is hidden).

    `collapsed` (Task 11 click-to-collapse) folds the whole panel down to
    ONE header row ending `▸` (screen space back on demand); `▾` when
    expanded. Both only render when `toggle` is given — the header then
    carries a 3-tuple MOUSE_UP handler (see _toggle_handler) that flips it.

    `forward` (W2 Task 8) is forwarded into every _item_handler/_toggle_handler
    built here — see their docstrings; it is the pane's
    `OutputPane.forward_mouse`, given first refusal on every row/header click
    so a drag armed on the pane above can still be extended/completed once
    it releases on this panel."""
    items = list(pending)
    rem = [r for r in (remote or ()) if isinstance(r, dict)]
    n = len(items)
    if not n and not rem:
        return []
    marker = "" if toggle is None else (" ▸" if collapsed else " ▾")
    header = ("class:qp.header", f" ⋯ queued ({n + len(rem)}){marker}")
    if toggle is not None:
        header = header + (_toggle_handler(toggle, forward),)
    frags = [header]
    if collapsed:
        return frags
    if n:
        frags.append(("class:qp.item", " · ↑ edit last · click to edit"))
    rstart = max(0, len(rem) - max_items)
    if rstart:
        frags.append(("class:qp.remote", f"\n   … +{rstart} more"))
    for r in rem[rstart:]:
        origin = str(r.get("origin") or "") or "remote"
        # A row surviving a marathon PARK (W1 front-3b) is still queued
        # server-side, not phantom -- the ⏸ prefix tells the user it's
        # waiting on a wake, not about to run right now.
        mark = "⏸ " if r.get("parked") else ""
        row = "\n   " + one_line(f"{mark}[{origin}] {r.get('text') or ''}",
                                 width - 4 if width > 0 else 0)
        frags.append(("class:qp.remote", row))
    start = max(0, n - max_items)
    if start:
        frags.append(("class:qp.item", f"\n   … +{start} more"))
    for i in range(start, n):
        style = "class:qp.last" if i == n - 1 else "class:qp.item"
        row = "\n   " + one_line(items[i], width - 4 if width > 0 else 0)
        if pull is None:
            frags.append((style, row))
        else:
            frags.append((style, row, _item_handler(pull, i, forward)))
    return frags
