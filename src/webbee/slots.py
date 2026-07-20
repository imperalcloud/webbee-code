"""Session slots — the browser-tab model (W4a). A SessionSlot is the atomic
per-tab world: the {agent, sink, pane} triple (wiring map §6 — created
together, never mixed) plus everything the wiring map §1 lists as a
must-become-per-slot singleton. SlotManager owns ordering + the active
pointer; it knows NOTHING of prompt_toolkit — pure and unit-testable.
WorkspaceResources caches the per-WORKSPACE pieces (intel, shadow, watcher,
git branch) shared by slots on the same repo root (map §6 boot split)."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque


@dataclass
class SessionSlot:
    kind: str                    # "home" | "session"
    workspace: str
    label: str                   # tab title (repo dir name; renamed by /rename later)
    pane: object                 # OutputPane (None never — Home has one too)
    sink: object | None          # RichSink; None for Home
    agent: object | None         # AgentSession; None for Home
    slot_id: str = ""            # W4b T5: "" = legacy id (tab 1 / fallback's only
                                  # slot -- preserves reattach-to-parked semantics);
                                  # every LATER session slot mints a short hex id
                                  # (_make_session_slot), threaded into the agent's
                                  # POST body + the steer poller's derived id.
    pending: deque = field(default_factory=deque)
    turn: dict = field(default_factory=lambda: {"task": None})
    pulled: dict = field(default_factory=lambda: {"text": "", "iid": ""})
    draft: str = ""                    # 0.3.24: per-tab unsent input -- stashed on switch-away,
                                        # restored on switch-back (browser-tab model: each tab
                                        # keeps its own form state); cleared on genuine submit.
    draft_cursor: int = 0              # cursor position within `draft`, restored alongside it
    qp_ui: dict = field(default_factory=lambda: {"collapsed": False})
    tp_ui: dict = field(default_factory=lambda: {"collapsed": False})
    mode: str = "default"
    git_branch: str = "-"
    history: object | None = None      # prompt_toolkit InMemoryHistory (made in tui task)
    steer_backlog: deque = field(default_factory=deque)   # reserved (W4b)
    bg_tasks: list = field(default_factory=list)          # per-slot cancellables
    _last_fill: float = 0.0            # Home only (Task 6): monotonic ts of the last fill_home run
    _filling: bool = False             # Home only (Task 6): a fill_home is in flight -- re-entrancy guard

    def status_glyph(self) -> str:
        """Tab glyph: ⚠ consent waiting beats ▶ busy beats ✓ idle; Home ◆.
        PURE given the sink's public accessors; None-sink (Home) → ◆."""
        if self.sink is None:
            return "◆"
        try:
            if self.sink.consent_pending():
                return "⚠"
            if self.sink.is_busy():
                return "▶"
        except Exception:
            pass
        return "○"


class SlotManager:
    def __init__(self) -> None:
        self.slots: list[SessionSlot] = []
        self.active_idx: int = 0

    def active(self) -> SessionSlot:
        return self.slots[max(0, min(self.active_idx, len(self.slots) - 1))]

    def add(self, slot: SessionSlot) -> int:
        self.slots.append(slot)
        return len(self.slots) - 1

    def switch(self, idx: int) -> bool:
        if 0 <= idx < len(self.slots) and idx != self.active_idx:
            self.active_idx = idx
            return True
        return False

    def close(self, idx: int) -> SessionSlot | None:
        """Close a SESSION tab (Home at 0 is never closable). The server-side
        run keeps living — closing a tab is a VIEW action (browser model);
        returns the removed slot so the caller cancels its bg_tasks."""
        if idx <= 0 or not (0 <= idx < len(self.slots)):
            return None
        slot = self.slots.pop(idx)
        if self.active_idx >= idx:
            self.active_idx = max(0, self.active_idx - 1)
        return slot

    def session_count(self) -> int:
        return sum(1 for s in self.slots if s.kind == "session")


def close_at(slots: SlotManager, idx: int, cancel_slot=None) -> bool:
    """The shared tab-close flow (W4a Task 5, generalized in Task 7 so a
    click can target ANY tab, not just the active one) — PT-free so tui's
    ✕/Ctrl-W handlers AND repl's `/close` command call the EXACT same
    function instead of growing separate copies of the same policy. Closes
    the tab AT `idx` via `slots.close(idx)` — Home (index 0) is already
    guarded there (never removable), so `idx == 0` is a safe no-op (returns
    False, `cancel_slot` never runs). `slots.close` itself already resolves
    the correct post-close `active_idx` no matter WHICH slot disappears
    (the closed tab itself, one before it, or one after it — see its own
    docstring), so this needs no idx-vs-active_idx branching at all.

    On a genuine close: `cancel_slot(removed)` runs first when given (repl's
    own callable — cancels the removed slot's OWN background tasks; the
    kernel's MarathonWorkflow keeps running server-side regardless, browser-
    tab model — `/new` against the same repo re-attaches later), then a short
    note lands in the now-ACTIVE (post-close) slot's sink, when it has one:
    Home has none, and a minimal test sink may not implement `.note` either,
    so this is `getattr`-guarded rather than assumed."""
    removed = slots.close(idx)
    if removed is None:
        return False
    if cancel_slot is not None:
        cancel_slot(removed)
    note = getattr(slots.active().sink, "note", None)
    if note is not None:
        note("tab closed — the run keeps going server-side; /new in that repo re-attaches")
    return True


def close_active(slots: SlotManager, cancel_slot=None) -> bool:
    """Thin wrapper (Task 7): close whichever tab is CURRENTLY active —
    Ctrl-W, Ctrl-D and repl's `/close` command all still mean "close what
    I'm looking at", so they keep calling this instead of resolving
    `slots.active_idx` themselves."""
    return close_at(slots, slots.active_idx, cancel_slot)


class WorkspaceResources:
    """Per-repo-root shared pieces (map §6): intel+watcher, shadow, git
    branch. Keyed by realpath of the repo root; get() returns the cached
    bundle or None (caller creates via the async boot helpers and put()s)."""

    def __init__(self) -> None:
        self._by_root: dict = {}

    def key(self, workspace: str) -> str:
        import os
        from webbee.repo import find_repo_root
        return os.path.realpath(find_repo_root(workspace))

    def get(self, workspace: str):
        return self._by_root.get(self.key(workspace))

    def put(self, workspace: str, bundle: dict) -> None:
        self._by_root[self.key(workspace)] = bundle

    def bundles(self) -> list[dict]:
        """PUBLIC accessor (Task 7 ledger hygiene) — every bundle this
        process has ever booted, one per distinct repo root, regardless of
        how many slots share it. Callers outside this class (repl's exit-time
        cancellation walk) must reach every watcher_task through THIS, never
        by poking `_by_root` directly."""
        return list(self._by_root.values())
