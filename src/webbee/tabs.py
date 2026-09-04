"""The tab bar — THE visible piece of the browser-in-terminal (W4a Task 4;
chip redesign 0.3.24 — Valentin: tabs were hard to notice and hard to
control, needed clear separators and uniform spacing; precise hit-zones +
✕/+ split + busy-close-confirm, 0.3.25, another live screenshot review;
neutral hover on tab chips, close buttons and new tab button, 0.4.2).
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
TAB_STYLE_HOVER = "class:tab.hover"
TAB_STYLE_ALERT = "class:tab.alert"
TAB_STYLE_CLOSE = "class:tab.close"
TAB_STYLE_CLOSE_HOVER = "class:tab.close.hover"
TAB_STYLE_CLOSE_ACTIVE = "class:tab.close.active"
TAB_STYLE_SEP = "class:tab.sep"
TAB_STYLE_NEW = "class:tab.new"
TAB_STYLE_NEW_HOVER = "class:tab.new.hover"

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


def _forward_consumed(res) -> bool:
    if res is NotImplemented:
        return False
    if res is False:
        return False
    return True


def _switch_handler(on_switch, idx: int, forward=None, on_hover=None):
    def _h(mouse_event):
        if forward is not None and _forward_consumed(forward(mouse_event)):
            return None
        from prompt_toolkit.mouse_events import MouseEventType
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if on_hover is not None:
                on_hover("tab", idx)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            on_switch(idx)
            return None
        return NotImplemented
    return _h


def _close_handler(on_close, idx: int, forward=None, on_hover=None):
    def _h(mouse_event):
        if forward is not None and _forward_consumed(forward(mouse_event)):
            return None
        from prompt_toolkit.mouse_events import MouseEventType
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if on_hover is not None:
                on_hover("close", idx)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            on_close(idx)
            return None
        return NotImplemented
    return _h


def _new_handler(on_new, forward=None, on_hover=None):
    def _h(mouse_event):
        if forward is not None and _forward_consumed(forward(mouse_event)):
            return None
        from prompt_toolkit.mouse_events import MouseEventType
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if on_hover is not None:
                on_hover("new", None)
            return None
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


def tab_fragments(slots, *, on_switch, on_close, on_new=None, width: int = 0,
                  forward=None, hover_target: str = "", hover_idx: "int | None" = None,
                  on_hover=None):
    """Render the row described in the module docstring with full hover support."""
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
        overhead = sum(len(p) + len(s) + 3 for _, _, p, s, _a in pieces)
        used = (len(home_text) + seps * len(_SEP) + overhead
               + len(_SEP) + len(_NEW_CHIP_TEXT))
        budget = max(0, width - used) // len(pieces)

    frags = []
    is_home_active = (active_idx == 0)
    is_home_hover = (hover_target == "tab" and hover_idx == 0 and not is_home_active)
    home_style = TAB_STYLE_ACTIVE if is_home_active else (
        TAB_STYLE_HOVER if is_home_hover else TAB_STYLE_IDLE)
    frags.append((home_style, home_text, _switch_handler(on_switch, 0, forward, on_hover)))

    for idx, slot, prefix, suffix, armed in pieces:
        frags.append((TAB_STYLE_SEP, _SEP))
        is_active = (idx == active_idx)
        glyph = suffix.strip()
        label = slot.label or ""
        if width > 0:
            label = _fit(label, budget)
        is_tab_hover = (hover_target == "tab" and hover_idx == idx and not is_active)
        if is_active:
            style = TAB_STYLE_ACTIVE
        elif is_tab_hover:
            style = TAB_STYLE_HOVER
        elif glyph == "⚠":
            style = TAB_STYLE_ALERT
        else:
            style = TAB_STYLE_IDLE

        frags.append((style, f"{prefix}{label}{suffix}", _switch_handler(on_switch, idx, forward, on_hover)))
        
        is_close_hover = (hover_target == "close" and hover_idx == idx)
        if armed:
            close_style, close_glyph = TAB_STYLE_ALERT, "✕?"
        else:
            if is_active:
                close_style = TAB_STYLE_CLOSE_ACTIVE
            elif is_close_hover:
                close_style = TAB_STYLE_CLOSE_HOVER
            else:
                close_style = TAB_STYLE_CLOSE
            close_glyph = "✕"
        frags.extend(_padded_control(close_style, close_glyph, _close_handler(on_close, idx, forward, on_hover)))

    frags.append((TAB_STYLE_SEP, _SEP))
    is_new_hover = (hover_target == "new")
    new_style = TAB_STYLE_NEW_HOVER if is_new_hover else TAB_STYLE_NEW
    frags.extend(_padded_control(new_style, "+", _new_handler(on_new, forward, on_hover)))

    return frags
