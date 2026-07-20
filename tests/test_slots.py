"""SessionSlot/SlotManager/WorkspaceResources (W4a) — the browser-tab
substrate. Pure, prompt_toolkit-free: status_glyph precedence, SlotManager
ordering/close semantics (Home at index 0 never closes), WorkspaceResources
same-repo-root sharing (wiring map §6 boot split)."""
import os
import subprocess

from webbee.slots import SessionSlot, SlotManager, WorkspaceResources, close_active


def _mk_repo(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


class _FakeSink:
    def __init__(self, consent=False, busy=False, raises=False):
        self._consent = consent
        self._busy = busy
        self._raises = raises

    def consent_pending(self):
        if self._raises:
            raise RuntimeError("boom")
        return self._consent

    def is_busy(self):
        if self._raises:
            raise RuntimeError("boom")
        return self._busy


def _mk_slot(kind="session", sink=None, label="t"):
    return SessionSlot(kind=kind, workspace=".", label=label, pane=object(), sink=sink, agent=None)


# --- status_glyph precedence ---

def test_status_glyph_home_is_diamond():
    slot = _mk_slot(kind="home", sink=None)
    assert slot.status_glyph() == "◆"


def test_status_glyph_consent_beats_busy():
    slot = _mk_slot(sink=_FakeSink(consent=True, busy=True))
    assert slot.status_glyph() == "⚠"


def test_status_glyph_busy_beats_idle():
    slot = _mk_slot(sink=_FakeSink(consent=False, busy=True))
    assert slot.status_glyph() == "▶"


def test_status_glyph_idle():
    slot = _mk_slot(sink=_FakeSink(consent=False, busy=False))
    assert slot.status_glyph() == "○"


def test_status_glyph_raising_sink_is_idle():
    slot = _mk_slot(sink=_FakeSink(raises=True))
    assert slot.status_glyph() == "○"


# --- SlotManager ---

def test_slot_manager_add_and_active():
    mgr = SlotManager()
    home = _mk_slot(kind="home")
    idx = mgr.add(home)
    assert idx == 0
    assert mgr.active() is home


def test_slot_manager_active_clamps_when_empty():
    mgr = SlotManager()
    mgr.slots.append(_mk_slot(kind="home"))
    mgr.active_idx = 5
    assert mgr.active() is mgr.slots[0]


def test_slot_manager_switch():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot())
    assert mgr.switch(1) is True
    assert mgr.active_idx == 1
    # switching to the already-active index is a no-op (returns False)
    assert mgr.switch(1) is False
    assert mgr.switch(99) is False
    assert mgr.active_idx == 1


def test_slot_manager_close_never_removes_home():
    mgr = SlotManager()
    home = _mk_slot(kind="home")
    mgr.add(home)
    assert mgr.close(0) is None
    assert len(mgr.slots) == 1
    assert mgr.slots[0] is home


def test_slot_manager_close_out_of_range_is_safe():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    assert mgr.close(-1) is None
    assert mgr.close(1) is None
    assert mgr.close(42) is None


def test_slot_manager_close_returns_removed_slot():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    victim = _mk_slot(label="victim")
    mgr.add(victim)
    removed = mgr.close(1)
    assert removed is victim
    assert len(mgr.slots) == 1


def test_slot_manager_close_adjusts_active_idx_when_closing_at_active():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(label="a"))
    mgr.add(_mk_slot(label="b"))
    mgr.active_idx = 2
    mgr.close(2)
    assert mgr.active_idx == 1
    assert len(mgr.slots) == 2


def test_slot_manager_close_adjusts_active_idx_when_closing_before_active():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(label="a"))
    mgr.add(_mk_slot(label="b"))
    mgr.active_idx = 2
    mgr.close(1)
    assert mgr.active_idx == 1
    assert mgr.slots[mgr.active_idx].label == "b"


def test_slot_manager_close_leaves_active_idx_when_closing_after_active():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(label="a"))
    mgr.add(_mk_slot(label="b"))
    mgr.active_idx = 1
    mgr.close(2)
    assert mgr.active_idx == 1
    assert mgr.slots[mgr.active_idx].label == "a"


def test_slot_manager_session_count_excludes_home():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(kind="session"))
    mgr.add(_mk_slot(kind="session"))
    assert mgr.session_count() == 2


# --- WorkspaceResources ---

def test_workspace_resources_same_root_shares_key(tmp_path):
    root = _mk_repo(tmp_path)
    res = WorkspaceResources()
    key_root = res.key(str(root))
    key_sub = res.key(str(root / "src"))
    assert key_root == key_sub
    assert key_root == os.path.realpath(str(root))


def test_workspace_resources_put_and_get_shared_across_subdirs(tmp_path):
    root = _mk_repo(tmp_path)
    res = WorkspaceResources()
    bundle = {"intel": object(), "shadow": object()}
    res.put(str(root), bundle)
    assert res.get(str(root / "src")) is bundle


def test_workspace_resources_get_missing_returns_none(tmp_path):
    root = _mk_repo(tmp_path)
    res = WorkspaceResources()
    assert res.get(str(root)) is None


# --- close_active (W4a Task 5 — shared tab-close flow, PT-free) ---


def test_close_active_guards_home_never_calls_cancel_slot():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    calls = []
    assert close_active(mgr, calls.append) is False
    assert calls == []
    assert len(mgr.slots) == 1


def test_close_active_closes_the_active_tab_and_adjusts_active_idx():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(label="a"))
    victim = _mk_slot(label="b")
    mgr.add(victim)
    mgr.active_idx = 2
    assert close_active(mgr, None) is True
    assert len(mgr.slots) == 2
    assert mgr.active_idx == 1
    assert mgr.slots[mgr.active_idx].label == "a"


def test_close_active_calls_cancel_slot_with_the_removed_slot():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    victim = _mk_slot(label="victim")
    mgr.add(victim)
    mgr.active_idx = 1
    seen = []
    assert close_active(mgr, seen.append) is True
    assert seen == [victim]


def test_close_active_notes_into_the_post_close_active_slots_sink():
    from types import SimpleNamespace
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    notes = []
    mgr.add(_mk_slot(label="a", sink=SimpleNamespace(note=notes.append)))
    mgr.add(_mk_slot(label="b"))
    mgr.active_idx = 2
    assert close_active(mgr, None) is True
    assert mgr.active_idx == 1                     # the note lands on "a", not "b"
    assert len(notes) == 1
    assert "server-side" in notes[0] and "/new" in notes[0]


def test_close_active_note_is_getattr_guarded_when_sink_has_none():
    # Home (sink=None) or a minimal test sink without .note must never crash
    # the close flow -- the note is a nice-to-have, not a hard dependency.
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(label="a", sink=_FakeSink()))  # _FakeSink has no .note
    mgr.add(_mk_slot(label="b"))
    mgr.active_idx = 2
    assert close_active(mgr, None) is True          # must not raise

    mgr2 = SlotManager()
    mgr2.add(_mk_slot(kind="home"))
    mgr2.add(_mk_slot(label="only-session"))
    mgr2.active_idx = 1
    assert close_active(mgr2, None) is True          # lands back on Home (sink=None)
    assert mgr2.active_idx == 0


def test_workspace_resources_distinct_roots_do_not_collide(tmp_path):
    root_a = _mk_repo(tmp_path / "a")
    root_b = _mk_repo(tmp_path / "b")
    res = WorkspaceResources()
    bundle_a = {"tag": "a"}
    bundle_b = {"tag": "b"}
    res.put(str(root_a), bundle_a)
    res.put(str(root_b), bundle_b)
    assert res.get(str(root_a)) is bundle_a
    assert res.get(str(root_b)) is bundle_b
