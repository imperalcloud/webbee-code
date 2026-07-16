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


def queue_height(pending) -> int:
    """PURE. Rows the panel needs: 1 header + one per SHOWN item + one
    `… +K more` row when the queue is deeper than QP_MAX_ITEMS. 0 when empty
    (the ConditionalContainer hides the panel then anyway). The cap keeps the
    output pane dominant on small terminals; the toolbar's `⋯N queued`
    segment stays the truth-teller for the full depth."""
    n = len(pending)
    if not n:
        return 0
    return 1 + min(n, QP_MAX_ITEMS) + (1 if n > QP_MAX_ITEMS else 0)


def pull_item(pending, buf, index: int) -> bool:
    """The ONE pull-to-edit implementation (serves BOTH the ↑ key — newest,
    index len(pending)-1 — and a panel-row click — that row's index): move
    pending[index] OUT of the queue and into the input buffer for editing,
    cursor at the end. Guards, identical on both paths: a buffer with ANY
    text is never clobbered, and a stale index (the queue drained between
    render and click) is ignored. Returns True when a pull happened (the
    caller invalidates)."""
    if buf.text or not (0 <= index < len(pending)):
        return False
    item = pending[index]
    del pending[index]
    buf.text = item
    buf.cursor_position = len(item)
    return True


def _item_handler(pull, index: int):
    """One row's mouse handler: MOUSE_UP (a click, not a drag/press) pulls
    THAT queued item into the input via `pull(index)`; every other event
    falls through (NotImplemented) so wheel scroll etc. keep today's
    behavior. Mirrors OutputPane._SelectControl's event discipline."""
    def _h(mouse_event):
        from prompt_toolkit.mouse_events import MouseEventType
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            pull(index)
            return None
        return NotImplemented
    return _h


def queue_fragments(pending, pull=None, width: int = 0):
    """PURE builder: the panel as prompt_toolkit formatted text, re-invoked
    every redraw (same live mechanics as the toolbar) so every queue
    add/edit/drain shows at once. Layout, top→bottom = drain order (FIFO —
    the TOP row runs next; the BOTTOM row is the newest, the one ↑ pulls,
    sitting right above the input):

        ⋯ queued (N) · ↑ edit last · click to edit
        … +K more            ← only when N > QP_MAX_ITEMS (the OLDEST hide)
        older item           ← muted (qp.item)
        newest item          ← accent (qp.last)

    When `pull` is given each item row is a 3-tuple fragment carrying a
    mouse handler that pulls exactly that item (see _item_handler). Empty
    queue → [] (the panel is hidden)."""
    items = list(pending)
    n = len(items)
    if not n:
        return []
    frags = [("class:qp.header", f" ⋯ queued ({n})"),
             ("class:qp.item", " · ↑ edit last · click to edit")]
    start = max(0, n - QP_MAX_ITEMS)
    if start:
        frags.append(("class:qp.item", f"\n   … +{start} more"))
    for i in range(start, n):
        style = "class:qp.last" if i == n - 1 else "class:qp.item"
        row = "\n   " + one_line(items[i], width - 4 if width > 0 else 0)
        if pull is None:
            frags.append((style, row))
        else:
            frags.append((style, row, _item_handler(pull, i)))
    return frags
