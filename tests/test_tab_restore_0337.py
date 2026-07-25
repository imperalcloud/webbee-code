"""0.3.37 — closing the terminal stops being destructive.

Three previously-missing behaviours, tested at their pure seams:
  * `tab_store`          — remember the tab layout per-repo, fail-soft.
  * `plan_tab_restore`   — reconcile the layout against what Temporal STILL
                           reports Running: re-attach vs restore-state-only.
  * `session_indicator`  — a PERSISTENT answer to "is a workflow running",
                           instead of one boot note that scrolls away.
  * `repl.restore_tabs`  — rebuild the remembered tabs through the ordinary
                           tab factory, degrading to fewer tabs on any error.
"""
import asyncio

import pytest

from webbee import tab_store
from webbee.active_sessions import (live_session_ids, plan_tab_restore,
                                    session_indicator)


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(tab_store, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tab_store, "_repo_key_for", lambda ws: "abc123")
    return tmp_path


# ── tab_store: the memory ─────────────────────────────────────────────────
def test_round_trip_remembers_the_layout(cache):
    tabs = [tab_store.tab_record(session_id="marathon-u-rabc123", label="webbee",
                                 mode="plan", workspace="/w", draft="half typed"),
            tab_store.tab_record(session_id="", label="scratch")]
    tab_store.save_tabs("/w", tabs)
    got = tab_store.load_tabs("/w")
    assert [t["label"] for t in got] == ["webbee", "scratch"]
    assert got[0]["session_id"] == "marathon-u-rabc123"
    assert got[0]["mode"] == "plan"
    assert got[0]["draft"] == "half typed"


def test_autopilot_is_never_remembered(cache):
    """Same safety rule as mode_store: autopilot auto-approves every tool
    call, so it must be re-chosen explicitly, never resumed off disk."""
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s1", mode="autopilot")])
    assert tab_store.load_tabs("/w")[0]["mode"] == "default"


def test_load_with_no_file_is_empty_not_an_error(cache):
    assert tab_store.load_tabs("/w") == []


def test_a_single_corrupt_line_costs_only_that_tab(cache):
    p = tab_store._path_for("/w")
    p_dir = __import__("os").path.dirname(p)
    __import__("os").makedirs(p_dir, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"session_id": "good1", "label": "keeps"}\n')
        fh.write('{not json at all\n')
        fh.write('{"session_id": "good2", "label": "also keeps"}\n')
    got = tab_store.load_tabs("/w")
    assert [t["label"] for t in got] == ["keeps", "also keeps"]


def test_save_failure_is_swallowed_not_raised(cache, monkeypatch):
    def _boom(*a, **k):
        raise OSError("read-only home")
    monkeypatch.setattr(tab_store, "_path_for", _boom)
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s")])   # must not raise


def test_the_tab_ceiling_is_enforced(cache):
    many = [tab_store.tab_record(session_id=f"s{i}") for i in range(tab_store.MAX_TABS + 9)]
    tab_store.save_tabs("/w", many)
    assert len(tab_store.load_tabs("/w")) <= tab_store.MAX_TABS


def test_clear_forgets_the_layout(cache):
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s")])
    tab_store.clear_tabs("/w")
    assert tab_store.load_tabs("/w") == []


# ── plan_tab_restore: attach vs state-only ────────────────────────────────
def test_a_still_running_session_is_marked_for_reattach():
    saved = [{"session_id": "live-1", "label": "a"}, {"session_id": "dead-1", "label": "b"}]
    plan = plan_tab_restore(saved, [{"session_id": "live-1"}])
    assert plan[0]["attach"] is True
    assert plan[1]["attach"] is False      # finished -> restore state only


def test_a_parked_session_carries_its_pending_consent_through():
    plan = plan_tab_restore([{"session_id": "s1"}],
                            [{"session_id": "s1", "pending_consent": True}])
    assert plan[0]["attach"] is True and plan[0]["pending_consent"] is True


def test_a_tab_that_never_ran_anything_restores_as_state_only():
    plan = plan_tab_restore([{"session_id": "", "label": "scratch"}], [])
    assert plan[0]["attach"] is False and plan[0]["label"] == "scratch"


def test_junk_records_are_ignored_not_trusted():
    assert plan_tab_restore(["not a dict", None, {"session_id": "ok"}], []) [0]["session_id"] == "ok"
    assert len(plan_tab_restore(["junk"], [])) == 0


def test_live_session_ids_skips_blank_ids():
    assert live_session_ids([{"session_id": "a"}, {"session_id": ""}, {}]) == {"a"}


# ── session_indicator: the persistent truth ───────────────────────────────
def test_nothing_running_says_nothing():
    assert session_indicator([], "abc") == ""


def test_own_repo_live_is_reported():
    out = session_indicator([{"session_id": "marathon-u-rabc"}], "abc")
    assert "live" in out


def test_own_repo_parked_mentions_the_approval():
    out = session_indicator([{"session_id": "marathon-u-rabc", "pending_consent": True}], "abc")
    assert "approval" in out


def test_only_other_repos_live_is_counted_separately():
    out = session_indicator([{"session_id": "marathon-u-rzzz"},
                             {"session_id": "marathon-u-ryyy"}], "abc")
    assert "2" in out and "elsewhere" in out


# ── restore_tabs: the orchestration ───────────────────────────────────────
class _Slot:
    def __init__(self):
        self.label = ""
        self.label_pinned = False
        self.draft = ""
        self.draft_cursor = 0


class _Slots:
    def __init__(self):
        self.slots = []
    def add(self, s):
        self.slots.append(s)
        return len(self.slots) - 1


def _run(coro):
    return asyncio.run(coro)


def test_restore_skips_the_first_record_because_boot_already_made_that_tab(cache):
    from webbee.repl import restore_tabs
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s1", label="one"),
                               tab_store.tab_record(session_id="s2", label="two")])
    slots = _Slots()
    notes = []
    opened = _run(restore_tabs("/w", slots, lambda rec: _mk(), note=notes.append,
                               sessions_fn=lambda: _wrap([{"session_id": "s2"}])))
    assert opened == 1                       # only the SECOND record
    assert slots.slots[0].label == "two"
    assert "restored 1 tab(s)" in notes[0] and "running server-side" in notes[0]


def test_a_lone_remembered_tab_restores_nothing(cache):
    from webbee.repl import restore_tabs
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s1")])
    assert _run(restore_tabs("/w", _Slots(), lambda rec: _mk())) == 0


def test_a_failing_tab_build_costs_one_tab_not_the_boot(cache):
    from webbee.repl import restore_tabs
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s1"),
                               tab_store.tab_record(session_id="s2", label="boom"),
                               tab_store.tab_record(session_id="s3", label="fine")])
    slots = _Slots()

    async def _make(rec):
        if rec.get("label") == "boom":
            raise RuntimeError("agent build failed")
        return _Slot()
    opened = _run(restore_tabs("/w", slots, _make))
    assert opened == 1 and slots.slots[0].label == "fine"


def test_a_gateway_that_cannot_list_sessions_still_restores_state(cache):
    from webbee.repl import restore_tabs
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s1"),
                               tab_store.tab_record(session_id="s2", label="two")])
    slots, notes = _Slots(), []

    async def _boom():
        raise OSError("gateway unreachable")
    opened = _run(restore_tabs("/w", slots, lambda rec: _mk(), sessions_fn=_boom,
                               note=notes.append))
    assert opened == 1
    assert "finished" in notes[0]        # honest: nothing confirmed running


def test_the_remembered_draft_comes_back(cache):
    from webbee.repl import restore_tabs
    tab_store.save_tabs("/w", [tab_store.tab_record(session_id="s1"),
                               tab_store.tab_record(session_id="s2", draft="unsent idea")])
    slots = _Slots()
    _run(restore_tabs("/w", slots, lambda rec: _mk()))
    assert slots.slots[0].draft == "unsent idea"
    assert slots.slots[0].draft_cursor == len("unsent idea")


async def _mk():
    return _Slot()


async def _wrap(v):
    return v


# ── snapshot_tabs: what gets remembered ───────────────────────────────────
def test_snapshot_skips_home_and_reads_the_session_id_off_the_agent():
    from webbee.repl import snapshot_tabs

    class _Agent:
        session_id = "marathon-u-rabc"

    class _S:
        def __init__(self, kind, label):
            self.kind, self.label = kind, label
            self.agent = _Agent()
            self.mode, self.workspace, self.draft = "plan", "/w", ""

    class _M:
        slots = [_S("home", "Home"), _S("session", "webbee")]
    got = snapshot_tabs(_M())
    assert len(got) == 1 and got[0]["label"] == "webbee"
    assert got[0]["session_id"] == "marathon-u-rabc"


def test_snapshot_tolerates_a_half_built_slot_during_shutdown():
    from webbee.repl import snapshot_tabs

    class _Bare:
        pass

    class _M:
        slots = [_Bare()]
    got = snapshot_tabs(_M())          # must not raise
    assert len(got) == 1 and got[0]["session_id"] == ""
