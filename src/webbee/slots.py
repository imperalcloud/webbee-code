"""Session slots — the browser-tab model (W4a). A SessionSlot is the atomic
per-tab world: the {agent, sink, pane} triple (wiring map §6 — created
together, never mixed) plus everything the wiring map §1 lists as a
must-become-per-slot singleton. SlotManager owns ordering + the active
pointer; it knows NOTHING of prompt_toolkit — pure and unit-testable.
WorkspaceResources caches the per-WORKSPACE pieces (intel, shadow, watcher,
git branch) shared by slots on the same repo root (map §6 boot split)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections import deque

# Auto-label (W4c T3): a session tab renames itself from its first task's
# text, browser-tab-title style. ANSI CSI sequences (colors/cursor moves --
# "\x1b[31m...") are stripped as a WHOLE run FIRST (the run's own bytes,
# ESC included, are never whitespace so collapsing later wouldn't touch
# them); whitespace is collapsed NEXT -- \s already covers tab/newline/CR,
# so a tab or newline inside the pasted text becomes a single space rather
# than being silently deleted (which would glue two words together); any
# STILL-remaining bare control byte (a lone ESC not part of a CSI run, NUL,
# etc. -- never a real word separator) is dropped LAST, after whitespace is
# already settled, so it simply vanishes without leaving a gap.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
AUTO_LABEL_MAX = 24


def _sanitize_whitespace(text: str) -> str:
    """PURE. The ANSI/control/whitespace cleanup shared by `auto_label` and
    `sanitize_label` below -- see the module comment above for the ordering
    rationale (ANSI runs first, then whitespace collapse, then leftover
    bare control bytes)."""
    cleaned = _ANSI_RE.sub("", text or "")
    cleaned = _WS_RE.sub(" ", cleaned)
    return _CONTROL_RE.sub("", cleaned).strip()


def auto_label(text: str) -> str:
    """PURE. A compact browser-tab-style title from `text` (a session's
    first task): sanitized (ANSI/control stripped -- see module comment
    above), internal whitespace collapsed to single spaces, then cut to
    `AUTO_LABEL_MAX` chars at the last whole word that still fits inside
    that budget (falling back to a hard cut only when no space exists in
    it at all -- same "no perfect boundary, still bounded" fallback
    `tabs._fit` uses) with a trailing `…` -- appended ONLY when the
    cleaned text was actually longer than the budget, never tacked onto
    something that already fit as-is. "" (empty/all-control/all-
    whitespace input) tells the caller to leave the slot's current label
    alone."""
    cleaned = _sanitize_whitespace(text)
    if not cleaned:
        return ""
    if len(cleaned) <= AUTO_LABEL_MAX:
        return cleaned
    cut = cleaned[:AUTO_LABEL_MAX]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


RENAME_LABEL_MAX = 32


def sanitize_label(text: str, max_len: int = RENAME_LABEL_MAX) -> str:
    """PURE. `/rename`'s own sanitizer (repl.py): the SAME ANSI/control/
    whitespace cleanup `auto_label` uses, but a plain hard cap at `max_len`
    chars -- no word-boundary search, no trailing `…`. The user explicitly
    typed this name; truncating it silently at a predictable length beats
    surprising them with an ellipsis they never asked for. "" either way on
    empty/all-noise input."""
    return _sanitize_whitespace(text)[:max_len]


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
    label_pinned: bool = False   # W4c T3: True once the label is LOCKED -- either
                                  # a manual /rename, or the one-shot auto-label
                                  # already fired (see repl._run_turn_on). Deliberately
                                  # the ONE flag for both: re-deriving "is this still
                                  # the default repo-basename label" from `workspace`
                                  # would misfire on an auto-isolated worktree slot
                                  # (its `workspace` is the worktree path, not the
                                  # original repo whose basename became the label).
    close_armed: bool = False    # W4c Part D: a ✕ click on a BUSY tab arms this
                                  # instead of closing outright (tui._close_tab_click);
                                  # a second click while armed closes for real. Any
                                  # switch or keypress disarms it (tui._disarm_all).
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


def is_turn_alive(slot: SessionSlot) -> bool:
    """PURE predicate (Part D — busy-tab close confirm): True iff `slot`'s
    OWN turn task (the same `turn["task"]` `_cancel_slot`/`_gate_busy`
    already read) is genuinely still running. Only the dock path ever
    populates `turn["task"]` at all -- a slot whose turn dict stays
    `{"task": None}` (Home, the fallback loop, a finished turn) is never
    considered busy."""
    task = slot.turn.get("task")
    return task is not None and not task.done()


def disarm_all(slots: SlotManager) -> None:
    """Part D: clear every slot's one-shot `close_armed` flag -- tui wires
    this into `_switch_to` (any tab switch) and the Application's
    `after_key_press` event (any keypress) alike, so a busy tab's armed
    "click ✕ again" state never lingers past the moment the user does
    anything else. Unconditional and idempotent -- cheaper to reset every
    slot than to track which one (if any) was armed."""
    for s in slots.slots:
        s.close_armed = False


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

    def roots(self) -> list[str]:
        """PUBLIC accessor (W5 Home recent-repos tile) — the realpath of every
        distinct repo root this process has booted a workspace for, insertion
        order. `bundles()` returns the per-root VALUE bundles; `roots()`
        returns their KEYS (the paths), which Home turns into one-click
        "open a new tab here" actions."""
        return list(self._by_root.keys())
