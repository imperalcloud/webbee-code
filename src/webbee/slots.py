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
    pending: deque = field(default_factory=deque)
    turn: dict = field(default_factory=lambda: {"task": None})
    pulled: dict = field(default_factory=lambda: {"text": "", "iid": ""})
    qp_ui: dict = field(default_factory=lambda: {"collapsed": False})
    tp_ui: dict = field(default_factory=lambda: {"collapsed": False})
    mode: str = "default"
    git_branch: str = "-"
    history: object | None = None      # prompt_toolkit InMemoryHistory (made in tui task)
    steer_backlog: deque = field(default_factory=deque)   # reserved (W4b)
    bg_tasks: list = field(default_factory=list)          # per-slot cancellables

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
