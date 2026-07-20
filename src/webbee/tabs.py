"""The tab bar — THE visible piece of the browser-in-terminal (W4a Task 4;
chip redesign 0.3.24 — Valentin: tabs were hard to notice and hard to
control, needed clear separators and uniform spacing; precise hit-zones +
✕/+ split + busy-close-confirm, 0.3.25, another live screenshot review).
`tab_fragments` is a PURE builder, queue_panel discipline (unit-tested
without an Application, no prompt_toolkit import at module top — only
inside the mouse handlers): it renders ONE row of padded CHIPS, each `"
{glyph} {label} "` — ONE leading + ONE trailing space baked INSIDE the
styled fragment itself, so every chip carries identical breathing room
regardless of style. Home is always first (glyph fixed ◆, label "Home",
NEVER a close ✕); every session slot is numbered by its own SlotManager
list index (the same index `slots.switch(idx)`/`slots.close(idx)` take) and
shaped `" {marker} {idx}·{label} {glyph} "` — `marker` (●/○) is THIS tab's
own active/inactive dot, `glyph` is `slot.status_glyph()` (⚠/▶/○). The
ACTIVE tab's chip is a SOLID bee-yellow block (`class:tab.active` —
background, not just text, so it's unmistakable at a glance); a NON-active
session tab whose glyph is ⚠ gets `class:tab.alert` (yellow text, no bg —
only the active chip owns a background, so the alert never competes with
it). A dim `" │ "` (`class:tab.sep`) sits between every pair of tabs, and
once more before the trailing + chip — exactly one separator per boundary,
never at the very start or the very end. Each tab's body is a 3-tuple
fragment (MOUSE_UP -> on_switch(idx), NotImplemented otherwise — wheel
keeps working, same event discipline as queue_panel._item_handler).

0.3.25 precise hit-zones: the ✕ (and the trailing + chip) are each split
into THREE fragments — an unclickable pad (" ", no mouse handler at all —
a bare 2-tuple), the glyph alone (1-2 chars, WITH the handler), another
unclickable pad — so a near-miss click on the padding does nothing instead
of firing the control underneath it (was: one merged `" ✕ "` fragment
where the whole run shared ONE handler). A BUSY tab's ✕ (its own turn task
still alive — `slots.is_turn_alive`) requires confirmation: the caller
(tui._close_tab_click) arms `slot.close_armed` on the first click instead
of closing, and this renderer then draws "✕?" in `class:tab.alert` until a
switch or keypress disarms it (tui._disarm_all) or a second click actually
closes. Unlike the queue/todo panels this bar is NEVER hidden — even a
single slot (Home alone) renders it; it IS the new look."""

TAB_STYLE_ACTIVE = "class:tab.active"
TAB_STYLE_IDLE = "class:tab"
TAB_STYLE_ALERT = "class:tab.alert"
TAB_STYLE_CLOSE = "class:tab.close"
TAB_STYLE_CLOSE_ACTIVE = "class:tab.close.active"
TAB_STYLE_SEP = "class:tab.sep"
TAB_STYLE_NEW = "class:tab.new"

_SEP = " │ "
_MIN_LABEL = 8
_NEW_CHIP_TEXT = " + "   # fixed width reserved from the label budget below


def _fit(label: str, max_len: int) -> str:
    """PURE. Middle-truncate `label` to at most `max_len` chars — but never
    below `_MIN_LABEL`: past that floor a shorter label reads as noise, not
    a tab title, so a very narrow terminal gets a row that overflows a
    little rather than an unreadable tab. A label already within the limit
    (or the floor, whichever is larger) is returned unchanged."""
    label = label or ""
    limit = max(max_len, _MIN_LABEL)
    if len(label) <= limit:
        return label
    if limit <= 1:
        return label[:1]
    head = -(-(limit - 1) // 2)   # ceil half to the head, the rest to the tail
    tail = limit - 1 - head
    return label[:head] + "…" + (label[-tail:] if tail > 0 else "")


def _switch_handler(on_switch, idx: int, forward=None):
    def _h(mouse_event):
        # FIX6: first refusal to a drag armed inside the output pane below
        # (`forward` = `pane.forward_mouse(ev, clamp="top")`) -- a release
        # that lands on the tab bar mid-drag completes the copy instead of
        # firing a switch/close underneath it. Consumed -> None (same
        # discipline as `tui._forwarding`); untouched -> fall through to
        # this handler's own MOUSE_UP dispatch, unchanged.
        if forward is not None and forward(mouse_event):
            return None
        from prompt_toolkit.mouse_events import MouseEventType
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            on_switch(idx)
            return None
        return NotImplemented
    return _h


def _close_handler(on_close, idx: int, forward=None):
    def _h(mouse_event):
        if forward is not None and forward(mouse_event):
            return None
        from prompt_toolkit.mouse_events import MouseEventType
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            on_close(idx)
            return None
        return NotImplemented
    return _h


def _new_handler(on_new, forward=None):
    def _h(mouse_event):
        if forward is not None and forward(mouse_event):
            return None
        from prompt_toolkit.mouse_events import MouseEventType
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            if on_new is not None:
                on_new()
            return None
        return NotImplemented
    return _h


def _padded_control(style: str, glyph: str, handler) -> list:
    """0.3.25 precise hit-zones: ONE control (✕ or +) as three fragments —
    unclickable pad, the bare glyph (the ONLY fragment carrying `handler`),
    unclickable pad. The pads are plain 2-tuples (style, text) -- prompt_
    toolkit fires NO mouse callback at all on a fragment with no third
    element, which is exactly "does nothing" for a near-miss click, no
    NotImplemented dance required."""
    return [(style, " "), (style, glyph, handler), (style, " ")]


def tab_fragments(slots, *, on_switch, on_close, on_new=None, width: int = 0, forward=None):
    """Render the row described in the module docstring. `slots` is a
    `SlotManager` (or anything shaped like one — `.slots` + `.active_idx`);
    slot 0 is always treated as Home. `width` (0 = unknown/no truncation —
    headless or pre-first-render) is divided evenly across every SESSION
    label after subtracting Home's own fixed chip text, the ` │ `
    separators, each session tab's own marker/index/glyph/padding/close
    overhead, and the trailing + chip's own reserved space, then each label
    is `_fit` to its share. Returns [] only when `slots` has no slots at all
    (never happens in practice — Home always occupies index 0). `forward`
    (FIX6, optional — `None` preserves every existing caller's behavior
    verbatim) is threaded into EVERY mouse handler this function hands out
    (Home's own switch handler and the trailing + chip included) so a drag
    armed in the output pane just below gets first refusal on every tab-bar
    click. `on_new` (0.3.25, optional — `None` means a click on + does
    nothing, same "no seam wired" contract as `forward`) fires with NO
    args on a genuine click; the + chip itself is ALWAYS rendered, exactly
    like a browser's own "open a new tab" button never disappears."""
    slot_list = slots.slots
    if not slot_list:
        return []
    active_idx = max(0, min(slots.active_idx, len(slot_list) - 1))
    home = slot_list[0]
    sessions = list(enumerate(slot_list[1:], start=1))

    home_text = f" ◆ {home.label or 'Home'} "
    seps = len(sessions)   # one separator before each session tab

    pieces = []   # (idx, slot, prefix, suffix, armed)
    for idx, slot in sessions:
        marker = "●" if idx == active_idx else "○"
        glyph = slot.status_glyph()
        armed = bool(getattr(slot, "close_armed", False))
        pieces.append((idx, slot, f" {marker} {idx}·", f" {glyph} ", armed))

    budget = 0
    if width > 0 and pieces:
        # Budget math treats every close control as its steady-state 3-char
        # width (" ✕ ") regardless of the (rare, transient) armed "✕?" state
        # -- a temporarily one-char-wider row while a busy-close confirm is
        # armed is a non-issue, not worth complicating this arithmetic over.
        overhead = sum(len(p) + len(s) + 3 for _, _, p, s, _a in pieces)
        used = (len(home_text) + seps * len(_SEP) + overhead
               + len(_SEP) + len(_NEW_CHIP_TEXT))
        budget = max(0, width - used) // len(pieces)

    frags = []
    home_style = TAB_STYLE_ACTIVE if active_idx == 0 else TAB_STYLE_IDLE
    frags.append((home_style, home_text, _switch_handler(on_switch, 0, forward)))

    for idx, slot, prefix, suffix, armed in pieces:
        frags.append((TAB_STYLE_SEP, _SEP))
        is_active = idx == active_idx
        glyph = suffix.strip()
        label = slot.label or ""
        if width > 0:
            label = _fit(label, budget)
        style = TAB_STYLE_ACTIVE if is_active else (
            TAB_STYLE_ALERT if glyph == "⚠" else TAB_STYLE_IDLE)
        frags.append((style, f"{prefix}{label}{suffix}", _switch_handler(on_switch, idx, forward)))
        if armed:
            close_style, close_glyph = TAB_STYLE_ALERT, "✕?"
        else:
            close_style = TAB_STYLE_CLOSE_ACTIVE if is_active else TAB_STYLE_CLOSE
            close_glyph = "✕"
        frags.extend(_padded_control(close_style, close_glyph, _close_handler(on_close, idx, forward)))

    frags.append((TAB_STYLE_SEP, _SEP))
    frags.extend(_padded_control(TAB_STYLE_NEW, "+", _new_handler(on_new, forward)))

    return frags
