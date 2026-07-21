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

# Trust/Security tile — public-facing product copy, subject to the ICNLI
# claims doctrine (Locus of Authority). Every line is a real, verifiable
# system property. CONFIRM against ~/Nextcloud/MCP-Marketing-Imperal/
# 07-voice-and-messaging.md + the exact docs URL on docs.imperal.io before
# ship (Task 11). Default below is concrete and defensible.
SECURITY_DOCS_URL = "https://docs.imperal.io/en/security/overview/"
SECURITY_LINES = (
    "Your private data is masked before the model sees it.",
    "Every risky or paid action asks you first.",
    "Encrypted in transit to auth.imperal.io.",
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
