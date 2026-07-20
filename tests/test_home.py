"""Home tab (W4a Task 6): the new-tab dashboard — identity/tabs/repo/system
tiles, async best-effort fill, is_stale/refill scheduling, and home_input's
"typing starts a session" flow."""
import asyncio
import inspect

from rich.console import Console

from webbee.account import Account
from webbee.home import (_mask_email, _pick_session_slot, fill_home,
                         is_stale, render_identity, render_repo_tile,
                         render_skeleton, render_slots_tile,
                         render_system_tile)
from webbee.render import WELCOME_HINT
from webbee.repl import _home_input, _home_target_workspace, _schedule_home_refill
from webbee.slots import SessionSlot, SlotManager, WorkspaceResources


class FakePane:
    """Stands in for `webbee.tui.OutputPane` — just enough for `fill_home`:
    a real Rich `Console(record=True)` (so `export_text()` works in
    assertions) plus a `notify()` counter (proves the pane was told to
    follow the tail after every repaint)."""

    def __init__(self, width: int = 80):
        self.console = Console(record=True, width=width)
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1


def _home_slot(workspace: str = "/ws") -> SessionSlot:
    return SessionSlot(kind="home", workspace=workspace, label="Home",
                       pane=FakePane(), sink=None, agent=None)


def _session_slot(workspace: str = "/ws", label: str = "ws", session_id: str = "") -> SessionSlot:
    from types import SimpleNamespace
    agent = SimpleNamespace(session_id=session_id)
    return SessionSlot(kind="session", workspace=workspace, label=label,
                       pane=FakePane(), sink=None, agent=agent)


# ---- _mask_email ------------------------------------------------------------

def test_mask_email_normal():
    assert _mask_email("valentin@webhostmost.com") == "v•••@w•••"


def test_mask_email_no_at():
    assert _mask_email("not-an-email") == "n•••"


def test_mask_email_empty():
    assert _mask_email("") == ""
    assert _mask_email(None) == ""


# ---- render_identity ---------------------------------------------------

def test_render_identity_masks_email_and_shows_nickname_plan():
    console = Console(record=True, width=80)
    account = Account(signed_in=True, email="valentin@webhostmost.com",
                      nickname="valentin", plan="pro", plan_status="active")
    render_identity(console, account)
    out = console.export_text()
    assert "@valentin" in out
    assert "pro plan" in out
    assert "v•••@w•••" in out
    assert "valentin@webhostmost.com" not in out   # raw address never printed (PII)


def test_render_identity_not_signed_in_renders_placeholder():
    console = Console(record=True, width=80)
    render_identity(console, None)
    out = console.export_text()
    assert "…" in out


# ---- render_skeleton -----------------------------------------------------

def test_render_skeleton_is_sync_and_paints_the_shell():
    assert not inspect.iscoroutinefunction(render_skeleton)
    console = Console(record=True, width=80)
    render_skeleton(console, 80)   # zero awaits -- a plain call, no event loop needed
    out = console.export_text()
    for name in ("Identity", "Tabs", "Repo", "System"):
        assert name in out
    assert "Home" in out
    assert "Ctrl+T" in out
    assert "Alt+N" in out


# ---- render_slots_tile / render_repo_tile / render_system_tile -----------

def test_render_slots_tile_shows_open_tabs_and_hint():
    slots = SlotManager()
    slots.add(_home_slot())
    slots.add(_session_slot(label="myrepo"))
    slots.active_idx = 1
    console = Console(record=True, width=80)
    render_slots_tile(console, slots)
    out = console.export_text()
    assert "myrepo" in out
    assert "Ctrl+T" in out


def test_render_slots_tile_empty_when_no_sessions():
    slots = SlotManager()
    slots.add(_home_slot())
    console = Console(record=True, width=80)
    render_slots_tile(console, slots)
    assert "no tabs open yet" in console.export_text()


def test_render_repo_tile_none_profile_is_placeholder():
    console = Console(record=True, width=80)
    render_repo_tile(console, None, "-", None)
    assert "…" in console.export_text()


def test_render_repo_tile_with_profile():
    console = Console(record=True, width=80)
    profile = {"languages": {"python": 10, "rust": 2}, "file_count": 42}
    render_repo_tile(console, profile, "main", "cp-1  abc123  now  label")
    out = console.export_text()
    assert "main" in out
    assert "42 files" in out
    assert "python" in out


def test_render_system_tile_no_live_session():
    console = Console(record=True, width=80)
    render_system_tile(console, remote_desc=None, update_notice=None)
    assert "no live session yet" in console.export_text()


def test_render_system_tile_with_remote_and_update():
    console = Console(record=True, width=80)
    render_system_tile(console, remote_desc="Remote control: ON", update_notice="upgrade available")
    out = console.export_text()
    assert "Remote control: ON" in out
    assert "upgrade available" in out


# ---- is_stale --------------------------------------------------------------

def test_is_stale_boundaries():
    slot = _home_slot()
    slot._last_fill = 1000.0
    assert is_stale(slot, 1000.0 + 300.0 + 0.01, ttl=300.0) is True
    assert is_stale(slot, 1000.0 + 300.0 - 0.01, ttl=300.0) is False
    assert is_stale(slot, 1000.0 + 300.0, ttl=300.0) is False   # exactly at ttl -- not yet stale


def test_is_stale_never_filled_defaults_stale():
    slot = _home_slot()   # _last_fill defaults to 0.0
    assert is_stale(slot, 1.0) is True


# ---- _pick_session_slot ---------------------------------------------------

def test_pick_session_slot_prefers_active_session():
    slots = SlotManager()
    slots.add(_home_slot())
    s1 = _session_slot(workspace="/one")
    slots.add(s1)
    slots.active_idx = 1
    assert _pick_session_slot(slots) is s1


def test_pick_session_slot_falls_back_to_most_recent_when_home_active():
    slots = SlotManager()
    slots.add(_home_slot())
    slots.add(_session_slot(workspace="/one"))
    s2 = _session_slot(workspace="/two")
    slots.add(s2)
    slots.active_idx = 0   # Home active
    assert _pick_session_slot(slots) is s2


def test_pick_session_slot_none_when_no_sessions():
    slots = SlotManager()
    slots.add(_home_slot())
    assert _pick_session_slot(slots) is None


# ---- fill_home --------------------------------------------------------------

def test_fill_home_raising_account_fetcher_still_renders_other_tiles():
    async def raising_fetcher(cfg, tp):
        raise RuntimeError("boom")

    home = _home_slot()
    slots = SlotManager()
    slots.add(home)
    session = _session_slot(label="myrepo")
    slots.add(session)
    slots.active_idx = 1

    asyncio.run(fill_home(
        home, cfg=None, token_provider=None, slots=slots,
        account_fetcher=raising_fetcher, sessions_client=None,
        resources=WorkspaceResources(), version="1.2.3"))

    out = home.pane.console.export_text()
    assert "myrepo" in out                 # the Tabs tile survived the identity fetch raising
    assert "System" in out
    assert home._last_fill > 0.0           # stamped on the way out regardless of the failure
    assert home._filling is False          # guard released
    assert WELCOME_HINT not in out         # never duplicates the session welcome splash


async def _fake_account_fetcher(cfg, tp):
    return Account(signed_in=True, email="v@w.com", nickname="v", plan="pro")


def test_fill_home_happy_path_renders_identity_and_guards_reentry():
    home = _home_slot()
    slots = SlotManager()
    slots.add(home)

    asyncio.run(fill_home(
        home, cfg=None, token_provider=None, slots=slots,
        account_fetcher=_fake_account_fetcher, sessions_client=None,
        resources=WorkspaceResources(), version="1.2.3"))

    out = home.pane.console.export_text()
    assert "@v" in out
    assert "pro plan" in out
    assert home.pane.notified > 0


def test_fill_home_guards_against_concurrent_overlap():
    calls = []

    async def counting_fetcher(cfg, tp):
        calls.append(1)
        await asyncio.sleep(0)
        return Account(signed_in=False)

    home = _home_slot()
    slots = SlotManager()
    slots.add(home)

    async def scenario():
        # Two overlapping fills started back-to-back: the second must see
        # `_filling` already True (set synchronously by the first, before
        # its own first await) and return immediately without fetching.
        t1 = asyncio.ensure_future(fill_home(
            home, cfg=None, token_provider=None, slots=slots,
            account_fetcher=counting_fetcher, sessions_client=None,
            resources=WorkspaceResources(), version="1.2.3"))
        t2 = asyncio.ensure_future(fill_home(
            home, cfg=None, token_provider=None, slots=slots,
            account_fetcher=counting_fetcher, sessions_client=None,
            resources=WorkspaceResources(), version="1.2.3"))
        await asyncio.gather(t1, t2)

    asyncio.run(scenario())
    assert len(calls) == 1


# ---- _home_target_workspace / _home_input ----------------------------------

def test_home_target_workspace_prefers_most_recent_session():
    slots = SlotManager()
    slots.add(_home_slot(workspace="/cwd"))
    slots.add(_session_slot(workspace="/one"))
    slots.add(_session_slot(workspace="/two"))
    assert _home_target_workspace(slots, "/cwd") == "/two"


def test_home_target_workspace_falls_back_to_cwd_with_no_sessions():
    slots = SlotManager()
    slots.add(_home_slot(workspace="/cwd"))
    assert _home_target_workspace(slots, "/cwd") == "/cwd"


def test_home_input_creates_slot_switches_and_runs_first_turn():
    slots = SlotManager()
    home = _home_slot(workspace="/cwd")
    slots.add(home)

    created = {}

    async def fake_make_session_slot(cfg, tp, ws, mode, *, resources, shared_client,
                                      agent_factory, intel_factory, shadow_factory, first):
        created["ws"] = ws
        created["first"] = first
        slot = _session_slot(workspace=ws, label="new")
        slot.sink = _EchoSink()
        return slot

    run_turn_calls = []

    async def fake_run_turn(slot, text):
        # FIX1: run_turn is slot-explicit now -- _home_input hands it the
        # NEW slot directly, never relying on slots.active() at call time.
        run_turn_calls.append((slot, text))

    import webbee.repl as repl_mod
    orig = repl_mod._make_session_slot
    repl_mod._make_session_slot = fake_make_session_slot
    try:
        asyncio.run(_home_input(
            "build a thing", slots=slots, cfg=None, token_provider=None, mode="default",
            resources=WorkspaceResources(), shared_client=None, agent_factory=None,
            intel_factory=None, shadow_factory=None, workspace="/cwd",
            ui_hooks={}, run_turn=fake_run_turn))
    finally:
        repl_mod._make_session_slot = orig

    assert created["first"] is False
    assert created["ws"] == "/cwd"          # no session tab existed yet -> falls back to cwd
    assert len(slots.slots) == 2
    assert slots.active_idx == 1            # switched into the new slot
    new_slot = slots.slots[1]
    assert new_slot.sink.echoed == ["build a thing"]   # the ❯ echo landed in the NEW slot
    assert run_turn_calls == [(new_slot, "build a thing")]    # the turn ran explicitly against the new slot


class _EchoSink:
    def __init__(self):
        self.echoed = []

    def user_echo(self, text):
        self.echoed.append(text)


def test_home_input_new_slot_sink_records_echo(monkeypatch):
    # Companion to the test above, isolating the echo/sink wiring: patch
    # `_make_session_slot` to hand back a slot whose sink is a bare
    # `_EchoSink` (no RichSink machinery needed) and confirm `_home_input`
    # calls `.user_echo` on THAT sink, not any other.
    slots = SlotManager()
    slots.add(_home_slot(workspace="/cwd"))

    async def fake_make_session_slot(cfg, tp, ws, mode, *, resources, shared_client,
                                      agent_factory, intel_factory, shadow_factory, first):
        s = _session_slot(workspace=ws)
        s.sink = _EchoSink()
        return s

    monkeypatch.setattr("webbee.repl._make_session_slot", fake_make_session_slot)

    async def fake_run_turn(slot, text):
        pass

    asyncio.run(_home_input(
        "hello", slots=slots, cfg=None, token_provider=None, mode="default",
        resources=WorkspaceResources(), shared_client=None, agent_factory=None,
        intel_factory=None, shadow_factory=None, workspace="/cwd",
        ui_hooks={}, run_turn=fake_run_turn))

    assert slots.slots[1].sink.echoed == ["hello"]


# ---- _schedule_home_refill --------------------------------------------------

def test_schedule_home_refill_ignores_non_home_idx():
    slots = SlotManager()
    slots.add(_home_slot())
    slots.add(_session_slot())
    assert _schedule_home_refill(slots, 1, {}) is False


def test_schedule_home_refill_noop_when_fresh():
    slots = SlotManager()
    home = _home_slot()
    home._last_fill = 1000.0
    slots.add(home)
    assert _schedule_home_refill(slots, 0, {}, now=1000.0 + 10.0) is False
    assert home.bg_tasks == []


def test_schedule_home_refill_schedules_when_stale():
    # `_schedule_home_refill` calls `asyncio.ensure_future` -- production
    # only ever calls it from inside a running Application loop (a
    # prompt_toolkit key/mouse handler), so the test needs one too.
    slots = SlotManager()
    home = _home_slot()
    home._last_fill = 1000.0
    slots.add(home)

    scheduled = []

    async def fake_fill_home(slot, **kw):
        scheduled.append(slot)

    async def scenario():
        ok = _schedule_home_refill(slots, 0, {}, now=1000.0 + 301.0, fill_home=fake_fill_home)
        assert ok is True
        assert len(home.bg_tasks) == 1
        await asyncio.gather(*home.bg_tasks)

    asyncio.run(scenario())
    assert scheduled == [home]


def test_switching_to_home_when_stale_schedules_exactly_one_refill():
    """The end-to-end contract (flag guard): even if the switch-to-Home
    hook fires twice in a row before the first fill has had a chance to
    update `_last_fill` (both calls see the SAME stale state and both
    schedule a bg task), `fill_home`'s own `_filling` guard means only ONE
    of them actually performs the fetch."""
    calls = []

    async def counting_fetcher(cfg, tp):
        calls.append(1)
        await asyncio.sleep(0)
        return Account(signed_in=False)

    slots = SlotManager()
    home = _home_slot()
    slots.add(home)   # _last_fill defaults to 0.0 -- maximally stale

    fill_kwargs = dict(cfg=None, token_provider=None, slots=slots,
                       account_fetcher=counting_fetcher, sessions_client=None,
                       resources=WorkspaceResources(), version="1.2.3")

    async def scenario():
        assert _schedule_home_refill(slots, 0, fill_kwargs, now=1000.0) is True
        assert _schedule_home_refill(slots, 0, fill_kwargs, now=1000.0) is True
        assert len(home.bg_tasks) == 2
        await asyncio.gather(*home.bg_tasks)

    asyncio.run(scenario())
    assert len(calls) == 1
