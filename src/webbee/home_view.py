"""Home dashboard (W5) — the interactive "website inside the terminal" that
replaces Home's flat skeleton. This module owns BOTH the pure, unit-testable
core (this section: snapshot dataclasses + the actionable-item model +
focus/nav/dispatch + pure builders, with NO prompt_toolkit import) AND the
`HomeView` render component below it (fragments + a focusable Window,
mirroring OutputPane's `.window`). `home.py` keeps the async fetch
orchestration and feeds a `HomeData` here; every interaction repaints via
`get_app().invalidate()`, never a `fill_home` re-run."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from webbee.account import Account
from webbee.wallet import Wallet

NOTIFY_OPTIONS = ("off", "panel", "tg", "both")
MODE_OPTIONS = ("default", "plan", "autopilot")

# Trust/Security tile — public-facing product copy, held to the ICNLI claims
# doctrine (Locus of Authority: anchor on WHO DECIDES, no overclaim, plain
# words, authority returns to the operator). Confirmed 2026-07-21 against
# ~/Nextcloud/MCP-Marketing-Imperal/07-voice-and-messaging.md; every line is
# a real, verifiable property. Line 1 keeps authority with the user (does not
# claim actions are blocked — Autopilot is the user's own choice); line 2 says
# "personal details" (PII), NOT "your data" (code IS sent to the model), which
# matches the claims-audited welcome copy in render.py. Docs URL verified live.
SECURITY_DOCS_URL = "https://docs.imperal.io/en/guides/audit-and-security/"
SECURITY_LINES = (
    "Risky and paid actions ask before they run — you stay in control.",
    "Personal details are masked before they reach the model.",
    "Encrypted in transit.",
)


@dataclass
class DeviceRow:
    label: str          # human surface name — never a raw id/IP (PII)
    current: bool = False


@dataclass
class TabRow:
    idx: int
    label: str
    mode: str
    glyph: str
    tokens: int
    credits: int
    active: bool


@dataclass
class HomeData:
    account: Account | None = None
    wallet: Wallet | None = None
    devices: list = field(default_factory=list)     # list[DeviceRow]
    recent: list = field(default_factory=list)      # list[str] repo root paths
    notify_state: str = ""       # "off"|"panel"|"tg"|"both"|"" (unknown)
    remote_desc: str = ""        # human line (display)
    new_tab_mode: str = "default"
    intel_enabled: bool = True
    intel_status: str = ""       # e.g. "42 files indexed" / ""
    endpoint: str = ""
    version: str = ""
    update_notice: str = ""
    notice: str = ""             # transient one-line note (top-up/security URL)


@dataclass
class ActionItem:
    id: str
    label: str
    hint: str
    activate: "Callable[[], None] | None" = None
    left: "Callable[[], None] | None" = None
    right: "Callable[[], None] | None" = None
    enabled: bool = True
    value: str = ""              # current segmented value (display only)


@dataclass
class HomeActions:
    new_session: "Callable[[], None]"
    open_recent: "Callable[[str], None]"
    switch_tab: "Callable[[int], None]"
    close_tab: "Callable[[int], None]"
    set_tab_mode: "Callable[[int, str], None]"
    set_notify: "Callable[[str], None]"
    set_new_tab_mode: "Callable[[str], None]"
    top_up: "Callable[[], None]"
    open_security_docs: "Callable[[], None]"


class HomeModel:
    """The ordered list of focusable items + focus/hover state + nav/dispatch.
    PURE. `focus_idx` indexes items in visual order; disabled items are
    rendered (greyed) but SKIPPED by nav and never dispatch."""

    def __init__(self, items: "list[ActionItem]"):
        self.items = items
        self.focus_idx = 0
        self.hover_id: "str | None" = None
        for i, it in enumerate(items):
            if it.enabled:
                self.focus_idx = i
                break

    def _enabled(self) -> "list[int]":
        return [i for i, it in enumerate(self.items) if it.enabled]

    def focused(self) -> "ActionItem | None":
        if 0 <= self.focus_idx < len(self.items):
            return self.items[self.focus_idx]
        return None

    def move(self, delta: int) -> None:
        order = self._enabled()
        if not order:
            return
        cur = self.focus_idx if self.focus_idx in order else order[0]
        self.focus_idx = order[(order.index(cur) + delta) % len(order)]

    def focus_next(self) -> None:
        self.move(1)

    def focus_prev(self) -> None:
        self.move(-1)

    def activate(self) -> None:
        it = self.focused()
        if it is not None and it.enabled and it.activate is not None:
            it.activate()

    def left(self) -> None:
        it = self.focused()
        if it is not None and it.enabled and it.left is not None:
            it.left()

    def right(self) -> None:
        it = self.focused()
        if it is not None and it.enabled and it.right is not None:
            it.right()

    def focus_id(self, item_id: "str | None") -> None:
        if item_id is None:
            return
        for i, it in enumerate(self.items):
            if it.id == item_id and it.enabled:
                self.focus_idx = i
                return


def _cycle(options, current: str, delta: int) -> str:
    try:
        i = options.index(current)
    except ValueError:
        i = 0
    return options[(i + delta) % len(options)]


def two_column(width: int, threshold: int = 100) -> bool:
    """Wide terminal -> You + Wallet render side-by-side; narrow -> stacked."""
    return width >= threshold


def tab_rows(slots) -> "list[TabRow]":
    """PURE transform of the live SlotManager into Home's per-tab rows
    (session tabs only -- Home never lists itself). Per-tab spend comes from
    the slot's own `sink.status()` ({tokens,credits}); a None sink or a
    status() that raises degrades to 0/0, never an exception."""
    rows: "list[TabRow]" = []
    active_idx = slots.active_idx
    for i, s in enumerate(slots.slots):
        if s.kind != "session":
            continue
        tokens = credits = 0
        sink = s.sink
        if sink is not None:
            try:
                st = sink.status()
                tokens = int(st.get("tokens", 0) or 0)
                credits = int(st.get("credits", 0) or 0)
            except Exception:
                pass
        rows.append(TabRow(idx=i, label=s.label or "", mode=s.mode,
                           glyph=s.status_glyph(), tokens=tokens,
                           credits=credits, active=(i == active_idx)))
    return rows


def build_home_model(data: "HomeData", tabs: "list[TabRow]",
                     actions: "HomeActions") -> "HomeModel":
    """Build the ordered actionable-item list. Notifications are DISABLED
    (greyed, nav-skipped) unless a session tab exists -- remote routing is
    per live session (spec §5). New-tab mode / top-up / security are always
    enabled (they need no live session)."""
    items: "list[ActionItem]" = []

    items.append(ActionItem(
        id="new-session", label="+ New session",
        hint="open a new session tab (Ctrl+T)",
        activate=actions.new_session))

    for t in tabs:
        items.append(ActionItem(
            id=f"tab-{t.idx}", label=t.label or f"tab {t.idx}",
            hint="switch to this tab",
            activate=(lambda idx=t.idx: actions.switch_tab(idx))))
        items.append(ActionItem(
            id=f"tab-mode-{t.idx}", label=t.mode, value=t.mode,
            hint="←→ change this tab's mode",
            left=(lambda idx=t.idx, cur=t.mode: actions.set_tab_mode(idx, _cycle(MODE_OPTIONS, cur, -1))),
            right=(lambda idx=t.idx, cur=t.mode: actions.set_tab_mode(idx, _cycle(MODE_OPTIONS, cur, +1)))))
        items.append(ActionItem(
            id=f"tab-close-{t.idx}", label="✕",
            hint="close this tab (the run keeps going server-side)",
            activate=(lambda idx=t.idx: actions.close_tab(idx))))

    for path in data.recent:
        name = path.rstrip("/").rsplit("/", 1)[-1] or path
        items.append(ActionItem(
            id=f"recent:{path}", label=name,
            hint=f"open a new tab in {path}",
            activate=(lambda p=path: actions.open_recent(p))))

    items.append(ActionItem(
        id="set-newtab-mode", label=data.new_tab_mode, value=data.new_tab_mode,
        hint="←→ change the mode new tabs open in",
        left=(lambda: actions.set_new_tab_mode(_cycle(MODE_OPTIONS, data.new_tab_mode, -1))),
        right=(lambda: actions.set_new_tab_mode(_cycle(MODE_OPTIONS, data.new_tab_mode, +1)))))

    has_session = bool(tabs)
    notify_val = data.notify_state or "off"
    items.append(ActionItem(
        id="set-notify", label=notify_val, value=notify_val,
        hint=("←→ where this session mirrors/steers"
              if has_session else "start a session first to route notifications"),
        left=(lambda: actions.set_notify(_cycle(NOTIFY_OPTIONS, notify_val, -1))),
        right=(lambda: actions.set_notify(_cycle(NOTIFY_OPTIONS, notify_val, +1))),
        enabled=has_session))

    items.append(ActionItem(
        id="top-up", label="Top up credits",
        hint="open the billing page to add credits",
        activate=actions.top_up))

    items.append(ActionItem(
        id="security-docs", label="Read our security & privacy →",
        hint="open the security & privacy documentation",
        activate=actions.open_security_docs))

    return HomeModel(items)


# ---- fragment layout helpers (PURE) ----------------------------------------

def _line_len(frags) -> int:
    return sum(len(f[1]) for f in frags)


def _flatten_lines(lines):
    """Flatten a list of fragment-lines into one fragment list with a newline
    fragment terminating each line (what a FormattedTextControl consumes)."""
    out = []
    for ln in lines:
        out.extend(ln)
        out.append(("", "\n"))
    return out


def _pad_line(frags, width: int):
    n = _line_len(frags)
    return list(frags) + ([("", " " * (width - n))] if n < width else [])


def _side_by_side(left, right, colw: int, gap: int = 3):
    """Zip two column line-lists into side-by-side fragment rows. Left column
    padded to `colw`; missing rows on either side render blank. ASCII-width
    only (Home avoids double-width glyphs so len()==cells)."""
    rows = []
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else []
        r = right[i] if i < len(right) else []
        rows.append(_pad_line(l, colw) + [("", " " * gap)] + r)
    return rows


def _fit(label: str, max_len: int) -> str:
    label = label or ""
    if len(label) <= max_len:
        return label
    if max_len <= 1:
        return label[:1]
    return label[:max_len - 1] + "…"


class HomeView:
    """Interactive Home. Owns render + interaction; the Home slot's `.pane`.
    Duck-types the OutputPane surface the dock's ticker/layout touch so
    `DynamicContainer(lambda: _pane().window)` (tui.py:1142) and `_tick_once`
    (tui.py:175-197) keep working with Home's pane in place unchanged."""

    def __init__(self, *, slots, actions: "HomeActions",
                 data: "HomeData | None" = None, width: int = 100, out_pane=None):
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType
        self.slots = slots
        self.actions = actions
        self.data = data if data is not None else HomeData()
        # Home is ONE ordinary scrollable Window (NOT dont_extend_height, NOT
        # an HSplit): when its content is taller than the viewport, prompt_
        # toolkit's own Window mouse handler scrolls it on the wheel (content
        # fragment handlers return NotImplemented for SCROLL, so it falls
        # through to Window._mouse_handler), and pageup/pagedown route here via
        # `scroll()`. `_say`/slash-command output is rendered INLINE at the
        # bottom of the SAME scrollable content (see `_fragments`), so it
        # scrolls with everything and never gets squeezed off a short terminal.
        # `out_pane` (built via `tui.OutputPane` by the dock, created first =
        # Home for the spy) stays purely as the CAPTURE backend: `_say` prints
        # into its console and `dump()` reads it back; its own window is NOT in
        # the layout. `self.console` IS that pane's console — it drives both the
        # captured command output AND the dashboard's column-width math.
        if out_pane is None:
            from webbee.output_pane import OutputPane
            out_pane = OutputPane(width=width)
        self._out = out_pane
        self.console = out_pane.console
        self._focus_id: "str | None" = None
        self._hover_id: "str | None" = None
        self._model: "HomeModel | None" = None
        # Virtualized scroll (mirrors OutputPane): the control is fed ONLY the
        # visible slice of lines, so it always fits the viewport and prompt_
        # toolkit never fights an (absent) cursor to reset the scroll — the
        # reason a plain FormattedTextControl window won't scroll. `_scroll` is
        # the top visible line; `_view_h_val` is the last render's viewport
        # height. Wheel adjusts `_scroll` here; PageUp/PageDown route via
        # `scroll()`; `notify()` jumps to the bottom to reveal new output.
        self._scroll = 0
        self._view_h_val = 20
        view = self

        class _HomeControl(FormattedTextControl):
            def mouse_handler(self, ev):
                et = ev.event_type
                if et == MouseEventType.SCROLL_DOWN:
                    view.scroll(3)
                    return None
                if et == MouseEventType.SCROLL_UP:
                    view.scroll(-3)
                    return None
                return super().mouse_handler(ev)

        self.control = _HomeControl(self._visible_fragments, focusable=True, show_cursor=False)
        self.window = Window(content=self.control, wrap_lines=False, always_hide_cursor=True)

    # ---- OutputPane-compatible surface (ticker/layout duck-type) ----------
    def reflow(self, new_width: int) -> None:
        # Keep the capture console's width in sync (drives the dashboard's
        # column math) and repaint; the Window re-lays-out at the new size.
        self._out.reflow(new_width)
        self._invalidate()

    def edge_tick(self) -> None:
        return None

    def flash(self) -> str:
        return self._out.flash()

    def scroll(self, delta: int) -> None:
        # Wheel (via the control) and PageUp/PageDown (tui routes here) move the
        # top visible line; clamped so you can't scroll past the ends.
        lines = self._all_lines()
        max_off = max(0, len(lines) - max(1, self._view_h_val))
        self._scroll = max(0, min(self._scroll + delta, max_off))
        self._invalidate()

    @property
    def _view_h(self) -> int:
        return self._view_h_val

    def forward_mouse(self, ev, clamp: str = "bottom") -> bool:
        return False

    def dump(self) -> str:
        return self._out.dump()

    def notify(self) -> None:
        # New captured output landed (a `_say` note / command reply). Reveal
        # it: jump to the bottom (clamped on the next render) so the answer is
        # visible, then repaint.
        self._scroll = 10 ** 7
        self._invalidate()

    def _invalidate(self) -> None:
        try:
            from prompt_toolkit.application import get_app_or_none
            app = get_app_or_none()
            if app is not None:
                app.invalidate()
        except Exception:
            pass

    # ---- model lifecycle --------------------------------------------------
    def _build_model(self) -> "HomeModel":
        tabs = tab_rows(self.slots)
        m = build_home_model(self.data, tabs, self.actions)
        m.focus_id(self._focus_id)                           # no-op if gone/disabled
        self._focus_id = m.focused().id if m.focused() is not None else None
        m.hover_id = self._hover_id
        self._model = m
        return m

    def move_focus(self, delta: int) -> None:
        m = self._build_model(); m.move(delta)
        self._focus_id = m.focused().id if m.focused() is not None else None
        self._invalidate()

    def focus_next(self) -> None:
        self.move_focus(1)

    def focus_prev(self) -> None:
        self.move_focus(-1)

    def activate_focused(self) -> None:
        self._build_model().activate()
        self._invalidate()

    def seg_left(self) -> None:
        self._build_model().left()
        self._invalidate()

    def seg_right(self) -> None:
        self._build_model().right()
        self._invalidate()

    # ---- mouse ------------------------------------------------------------
    def _item_handler(self, item: "ActionItem"):
        def _h(mouse_event):
            from prompt_toolkit.mouse_events import MouseEventType
            et = mouse_event.event_type
            if et == MouseEventType.MOUSE_MOVE:
                if self._hover_id != item.id:
                    self._hover_id = item.id
                    self._invalidate()
                return None
            if et == MouseEventType.MOUSE_UP:
                if not item.enabled:
                    return None
                self._focus_id = item.id
                if item.activate is not None:
                    item.activate()
                elif item.right is not None:
                    item.right()
                self._invalidate()
                return None
            return NotImplemented
        return _h

    # ---- render -----------------------------------------------------------
    def _all_lines(self):
        from webbee.home import _mask_email
        from webbee.render import _fmt_tokens
        m = self._build_model()
        by_id = {it.id: it for it in m.items}
        focused = m.focused()
        focus_id = focused.id if focused is not None else None
        hover_id = m.hover_id
        width = max(20, self.console.width or 80)

        def sfor(item):
            if item.id == focus_id or item.id == hover_id:
                return "class:home.focus"
            if not item.enabled:
                return "class:home.disabled"
            return "class:home.item"

        def act(item, text):
            return (sfor(item), text, self._item_handler(item))

        def hdr(title):
            return [("class:home.header", f" {title} ")]

        def you_lines():
            a = self.data.account
            L = [hdr("You")]
            if a is None or not getattr(a, "signed_in", False):
                return L + [[("class:home.dim", "  …")]]
            nick = getattr(a, "nickname", "") or ""
            L.append([("class:home.value", f"  @{nick}" if nick else "  (no nickname)")])
            plan = getattr(a, "plan", "") or ""
            status = getattr(a, "plan_status", "") or ""
            if plan:
                tag = f" ({status})" if status and status != "active" else ""
                L.append([("class:home.dim", f"  {plan} plan{tag}")])
            renews = getattr(a, "plan_renews", "") or ""
            if renews:
                # `plan_renews` is the formatted subscription expires_at: an
                # auto-renewing (active/trialing) plan RENEWS then, a
                # cancelled/lapsing one EXPIRES then.
                verb = "renews" if status in ("active", "trialing", "") else "expires"
                L.append([("class:home.dim", f"  {verb} {renews}")])
            email = getattr(a, "email", "") or ""
            if email:
                L.append([("class:home.dim", f"  {_mask_email(email)}")])
            return L

        def wallet_lines():
            w = self.data.wallet
            L = [hdr("Wallet")]
            if w is None:
                L.append([("class:home.dim", "  credits —")])
            else:
                cap = f" / {_fmt_tokens(w.cap)}" if w.cap else ""
                L.append([("class:home.value", f"  {_fmt_tokens(w.balance)}{cap} credits")])
            it = by_id["top-up"]
            L.append([("class:home.dim", "  "), act(it, "Top up credits")])
            return L

        def start_lines():
            it = by_id["new-session"]
            return [hdr("Start"),
                    [("class:home.dim", "  "), act(it, "+ New session")],
                    [("class:home.dim", "  or type a task here to open a tab and start")]]

        def tabs_lines():
            L = [hdr("Your tabs")]
            tabs = tab_rows(self.slots)
            if not tabs:
                return L + [[("class:home.dim", "  no tabs open yet — type a task to start")]]
            for t in tabs:
                sw, md, cl = by_id[f"tab-{t.idx}"], by_id[f"tab-mode-{t.idx}"], by_id[f"tab-close-{t.idx}"]
                marker = "●" if t.active else "○"
                L.append([
                    ("class:home.dim", f"  {marker} {t.idx} "),
                    act(sw, _fit(t.label or f"tab {t.idx}", 24)),
                    ("class:home.dim", f"  {t.glyph}  "),
                    act(md, f"[{md.value}]"),
                    ("class:home.dim", f"  {_fmt_tokens(t.tokens)} tok · {_fmt_tokens(t.credits)} cr  "),
                    act(cl, "✕"),
                ])
            return L

        def recent_lines():
            L = [hdr("Recent repos")]
            if not self.data.recent:
                return L + [[("class:home.dim", "  —")]]
            for path in self.data.recent:
                it = by_id[f"recent:{path}"]
                name = _fit(path.rstrip("/").rsplit("/", 1)[-1] or path, 28)
                L.append([("class:home.dim", "  ▸ "), act(it, name)])
            return L

        def devices_lines():
            L = [hdr("Your devices")]
            if not self.data.devices:
                return L + [[("class:home.dim", "  …")]]
            for d in self.data.devices:
                tag = " (this one)" if d.current else ""
                L.append([("class:home.dim", f"  • {_fit(d.label, 32)}{tag}")])
            return L

        def settings_lines():
            L = [hdr("Settings")]
            ntm, nt = by_id["set-newtab-mode"], by_id["set-notify"]
            L.append([("class:home.dim", "  new tabs open in  "), act(ntm, f"[{ntm.value}]")])
            L.append([("class:home.dim", "  notifications     "),
                      (sfor(nt), f"[{nt.value}]", self._item_handler(nt))])
            d = self.data
            intel = d.intel_status or ("on" if d.intel_enabled else "off")
            L.append([("class:home.dim", f"  code intel {intel} · {d.endpoint} · v{d.version}")])
            if d.update_notice:
                L.append([("class:home.dim", f"  {d.update_notice}")])
            return L

        def security_lines():
            it = by_id["security-docs"]
            L = [hdr("Trust & security")]
            for line in SECURITY_LINES:
                L.append([("class:home.dim", f"  {line}")])
            L.append([("class:home.dim", "  "), act(it, "Read our security & privacy →")])
            return L

        def footer_lines():
            hint = ""
            if hover_id and hover_id in by_id:
                hint = by_id[hover_id].hint
            elif focused is not None:
                hint = focused.hint
            L = [[("", "")], [("class:home.hint", f"  {hint}")]]
            if self.data.notice:
                L.append([("class:home.dim", f"  {self.data.notice}")])
            L.append([("class:home.dim",
                       "  ↑↓ move · ↵ open · ←→ change "
                       "· Ctrl+T new tab · Alt+N switch")])
            return L

        # A small centered "◆ Home" title. The Webbee Code logo now opens each
        # NEW session tab instead (RichSink.banner), so Home itself stays clean.
        title_pad = max(0, (width - len("◆ Home")) // 2)
        lines = [[("", "")], [("", " " * title_pad), ("class:home.header", "◆ Home")], [("", "")]]
        if two_column(width):
            colw = max(20, (width // 2) - 3)
            lines += _side_by_side(you_lines(), wallet_lines(), colw)
        else:
            lines += you_lines() + [[("", "")]] + wallet_lines()
        lines += [[("", "")]] + start_lines()
        lines += [[("", "")]] + tabs_lines()
        lines += [[("", "")]] + recent_lines()
        lines += [[("", "")]] + devices_lines()
        lines += [[("", "")]] + settings_lines()
        lines += [[("", "")]] + security_lines()
        lines += footer_lines()

        # `_say` / slash-command output, captured in the backend pane, rendered
        # INLINE at the very bottom of the same scrollable content (ANSI →
        # fragments, styling preserved). `notify()` jumps the scroll here so a
        # command's answer is visible; it scrolls with the rest of Home.
        dump = self._out.dump()
        if dump.strip():
            from prompt_toolkit.formatted_text import ANSI, to_formatted_text
            lines.append([("", "")])
            lines.append([("class:home.header", " Output ")])
            lines.append(list(to_formatted_text(ANSI(dump.rstrip("\n")))))
        return lines

    def _fragments(self):
        """The FULL dashboard flattened (every line). The build helpers and
        tests use this; the on-screen control uses `_visible_fragments`."""
        return _flatten_lines(self._all_lines())

    def _visible_fragments(self):
        """Only the visible slice `[_scroll : _scroll+view_h]`, flattened — what
        the control renders. Captures the live viewport height from the last
        render (`render_info.window_height`) and clamps `_scroll` into range, so
        the slice always fits and the window never needs (and never fights) a
        cursor to scroll."""
        ri = getattr(self.window, "render_info", None)
        if ri is not None and getattr(ri, "window_height", 0):
            self._view_h_val = ri.window_height
        lines = self._all_lines()
        h = max(1, self._view_h_val)
        off = max(0, min(self._scroll, max(0, len(lines) - h)))
        self._scroll = off
        return _flatten_lines(lines[off:off + h])
