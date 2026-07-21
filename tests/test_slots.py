"""SessionSlot/SlotManager/WorkspaceResources (W4a) — the browser-tab
substrate. Pure, prompt_toolkit-free: status_glyph precedence, SlotManager
ordering/close semantics (Home at index 0 never closes), WorkspaceResources
same-repo-root sharing (wiring map §6 boot split)."""
import os
import subprocess

from webbee.slots import (SessionSlot, SlotManager, WorkspaceResources,
                          auto_label, close_active, close_at, disarm_all,
                          is_turn_alive, sanitize_label)


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


def test_workspace_resources_bundles_returns_every_cached_bundle(tmp_path):
    # PUBLIC accessor (Task 7 ledger hygiene) -- the exit-time cancellation
    # walk must reach every bundle through THIS, never `_by_root` directly.
    root_a = _mk_repo(tmp_path / "a")
    root_b = _mk_repo(tmp_path / "b")
    res = WorkspaceResources()
    res.put(str(root_a), {"tag": "a"})
    res.put(str(root_b), {"tag": "b"})
    assert {b["tag"] for b in res.bundles()} == {"a", "b"}


def test_workspace_resources_bundles_empty_when_nothing_cached():
    assert WorkspaceResources().bundles() == []


# --- close_at (W4a Task 7 -- close_active generalized to an explicit idx) ---


def test_close_at_home_is_guarded_never_calls_cancel_slot():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    calls = []
    assert close_at(mgr, 0, calls.append) is False
    assert calls == []
    assert len(mgr.slots) == 1


def test_close_at_closes_a_background_tab_without_disturbing_the_active_one():
    # ✕ on a BACKGROUND tab (idx != active_idx) closes THAT tab; the active
    # tab survives, with its index adjusted down when the removed tab sat
    # before it.
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    victim = _mk_slot(label="a")
    mgr.add(victim)
    survivor = _mk_slot(label="b")
    mgr.add(survivor)
    mgr.active_idx = 2                          # active = b
    seen = []
    assert close_at(mgr, 1, seen.append) is True   # close a (background)
    assert seen == [victim]
    assert len(mgr.slots) == 2
    assert mgr.slots[mgr.active_idx] is survivor   # still looking at b
    assert mgr.active_idx == 1                     # index shifted down


def test_close_at_closing_the_active_tab_matches_close_active():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(label="a"))
    victim = _mk_slot(label="b")
    mgr.add(victim)
    mgr.active_idx = 2
    seen = []
    assert close_at(mgr, mgr.active_idx, seen.append) is True
    assert seen == [victim]
    assert mgr.active_idx == 1
    assert mgr.slots[mgr.active_idx].label == "a"


def test_close_active_is_a_thin_wrapper_over_close_at():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    mgr.add(_mk_slot(label="a"))
    victim = _mk_slot(label="b")
    mgr.add(victim)
    mgr.active_idx = 2
    seen = []
    assert close_active(mgr, seen.append) is True
    assert seen == [victim]
    assert mgr.active_idx == 1


# --- auto_label (W4c T3: tabs name themselves after the first task) --------


def test_auto_label_short_text_passes_through_unchanged():
    assert auto_label("fix the bug") == "fix the bug"


def test_auto_label_collapses_internal_whitespace():
    assert auto_label("fix   the\n\tbug") == "fix the bug"


def test_auto_label_strips_leading_trailing_whitespace():
    assert auto_label("   fix the bug   ") == "fix the bug"


def test_auto_label_cuts_at_word_boundary_under_the_cap():
    text = "please fix the authentication bug in the login flow"
    label = auto_label(text)
    assert len(label) <= 25          # 24-char budget + the ellipsis
    assert label.endswith("…")
    assert not label[:-1].endswith(" ")   # trailing space trimmed before the ellipsis
    assert text.startswith(label[:-1].rstrip())


def test_auto_label_no_ellipsis_when_text_fits_exactly_at_the_cap():
    text = "x" * 24
    assert auto_label(text) == text


def test_auto_label_hard_cuts_a_single_word_with_no_boundary():
    text = "x" * 40
    label = auto_label(text)
    assert label == "x" * 24 + "…"


def test_auto_label_strips_ansi_color_codes():
    assert auto_label("\x1b[31mfix the bug\x1b[0m") == "fix the bug"


def test_auto_label_strips_bare_control_bytes():
    assert auto_label("fix\x00the\x1bbug") == "fixthebug"


def test_auto_label_empty_or_whitespace_only_returns_empty():
    assert auto_label("") == ""
    assert auto_label("   \n\t  ") == ""
    assert auto_label(None) == ""


# --- sanitize_label (/rename: cap 32, no ellipsis) --------------------------

def test_sanitize_label_short_name_passes_through():
    assert sanitize_label("billing") == "billing"


def test_sanitize_label_collapses_whitespace_and_trims():
    assert sanitize_label("  billing   fix  ") == "billing fix"


def test_sanitize_label_hard_caps_at_32_no_ellipsis():
    name = "x" * 40
    result = sanitize_label(name)
    assert result == "x" * 32
    assert "…" not in result


def test_sanitize_label_custom_max_len():
    assert sanitize_label("x" * 10, max_len=5) == "x" * 5


def test_sanitize_label_strips_ansi_and_control():
    assert sanitize_label("\x1b[31mbilling\x1b[0m\x00") == "billing"


def test_sanitize_label_empty_or_whitespace_only_returns_empty():
    assert sanitize_label("") == ""
    assert sanitize_label("   ") == ""


# --- is_turn_alive (Part D: busy-tab close confirm) -------------------------

class _FakeTask:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


def test_is_turn_alive_false_when_no_task_recorded():
    slot = _mk_slot()
    assert is_turn_alive(slot) is False


def test_is_turn_alive_true_while_task_running():
    slot = _mk_slot()
    slot.turn["task"] = _FakeTask(done=False)
    assert is_turn_alive(slot) is True


def test_is_turn_alive_false_once_task_is_done():
    slot = _mk_slot()
    slot.turn["task"] = _FakeTask(done=True)
    assert is_turn_alive(slot) is False


# --- disarm_all (Part D) -----------------------------------------------------

def test_disarm_all_clears_every_slots_flag():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    a = _mk_slot(label="a")
    a.close_armed = True
    b = _mk_slot(label="b")
    b.close_armed = True
    mgr.add(a)
    mgr.add(b)
    disarm_all(mgr)
    assert a.close_armed is False and b.close_armed is False


def test_disarm_all_is_a_noop_when_nothing_armed():
    mgr = SlotManager()
    mgr.add(_mk_slot(kind="home"))
    disarm_all(mgr)   # must not raise


def test_workspace_resources_roots_lists_booted_paths(monkeypatch):
    import webbee.repo as repo_mod
    from webbee.slots import WorkspaceResources
    monkeypatch.setattr(repo_mod, "find_repo_root", lambda ws: ws)
    monkeypatch.setattr("os.path.realpath", lambda p: p)
    res = WorkspaceResources()
    res.put("/a", {"git_branch": "main"})
    res.put("/b", {"git_branch": "dev"})
    assert res.roots() == ["/a", "/b"]
    assert res.bundles() == [{"git_branch": "main"}, {"git_branch": "dev"}]
