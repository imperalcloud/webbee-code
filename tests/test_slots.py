"""SessionSlot/SlotManager/WorkspaceResources (W4a) — the browser-tab
substrate. Pure, prompt_toolkit-free: status_glyph precedence, SlotManager
ordering/close semantics (Home at index 0 never closes), WorkspaceResources
same-repo-root sharing (wiring map §6 boot split)."""
import os
import subprocess

from webbee.slots import SessionSlot, SlotManager, WorkspaceResources


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
