"""The STICKY todo panel pinned above the queue panel (0.3.15 — replaces the
inline scroll-away checklist in the dock: the plan now stays visible and
updates in place on every `todo` frame, mirroring the proven queue-panel
pattern). The builders here are PURE (unit-tested without an Application);
tui.run_session mounts them in a ConditionalContainer that occupies ZERO rows
while the list is empty, so the todo-less dock is pixel-identical to before.
The panel reads the sink-owned `RichSink.current_todos` list object every
redraw (in-place mutation, the twin of `remote_pending`); the scrollback
record is the ONE inline checklist RichSink.end_turn prints per turn. Split
into its own file like queue_panel.py to stay far under the size ceiling."""

from webbee.queue_panel import one_line

TP_MAX_ITEMS = 6   # item rows shown; completed collapse first, then `… +K more`


def _rows(todos) -> list:
    """PURE. Normalize to (status, content) pairs: dict items with a
    non-empty content only — malformed entries are skipped, never raised on
    (same defensive contract as the inline checklist render)."""
    out = []
    for t in (todos if isinstance(todos, (list, tuple)) else ()):
        if not isinstance(t, dict):
            continue
        content = str(t.get("content", "") or "").strip()
        if not content:
            continue
        out.append((str(t.get("status", "") or ""), content))
    return out


def todo_fragments(todos, width: int = 0):
    """PURE builder: the panel as prompt_toolkit formatted text, re-invoked
    every redraw (same live mechanics as the queue panel) so every todo_write
    republish shows at once. Layout, top→bottom = plan order:

        📋 Todos (done/total)
        … ✓K done            ← completed collapse FIRST when over the cap
        ✓ finished item      ← green glyph, dim struck text
        ▶ current item       ← bold bee-yellow — NEVER hidden by the cap
        ○ pending item       ← muted
        … +K more            ← pending overflow beyond TP_MAX_ITEMS

    Counts derive from the items themselves (the kernel republishes the FULL
    list on every todo_write). Empty/malformed-only list → [] (panel hidden)."""
    rows = _rows(todos)
    if not rows:
        return []
    done = sum(1 for s, _ in rows if s == "completed")
    frags = [("class:tp.header", f" 📋 Todos ({done}/{len(rows)})")]
    show = rows
    hidden_done = 0
    if len(show) > TP_MAX_ITEMS:
        # History compresses first: collapse the completed items into one
        # summary row so the live part of the plan keeps its rows.
        active = [r for r in show if r[0] != "completed"]
        hidden_done = len(show) - len(active)
        show = active
    extra = 0
    if len(show) > TP_MAX_ITEMS:
        cut = show[TP_MAX_ITEMS:]
        extra = len(cut)
        show = show[:TP_MAX_ITEMS]
        cur = next((r for r in cut if r[0] == "in_progress"), None)
        if cur is not None:
            show[-1] = cur   # what's happening NOW is never hidden by the cap
    if hidden_done:
        frags.append(("class:tp.done", f"\n   … ✓{hidden_done} done"))
    for status, content in show:
        text = one_line(content, width - 5 if width > 0 else 0)
        if status == "completed":
            frags.append(("class:tp.done", "\n   ✓ "))
            frags.append(("class:tp.done.text", text))
        elif status == "in_progress":
            frags.append(("class:tp.now", "\n   ▶ " + text))
        else:                              # pending / unknown -> not started yet
            frags.append(("class:tp.item", "\n   ○ " + text))
    if extra:
        frags.append(("class:tp.item", f"\n   … +{extra} more"))
    return frags


def todo_height(todos) -> int:
    """PURE. Rows the panel needs — derived from the SAME fragments the panel
    renders (header line + one per newline), so height can never desync from
    the visible rows. 0 when the list is empty (the ConditionalContainer
    hides the panel then anyway)."""
    frags = todo_fragments(todos)
    if not frags:
        return 0
    return 1 + sum(f[1].count("\n") for f in frags)
