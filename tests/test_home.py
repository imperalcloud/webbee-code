"""Home tab (W4a Task 6): the new-tab dashboard — identity/tabs/repo/system
tiles, async best-effort fill, is_stale/refill scheduling, and home_input's
"typing starts a session" flow."""
import asyncio

from webbee.account import Account
from webbee.home import (_mask_email, _notify_state_from, _pick_session_slot,
                         fill_home, is_stale)
from webbee.repl import _home_input, _home_target_workspace, _schedule_home_refill
from webbee.slots import SessionSlot, SlotManager, WorkspaceResources
from webbee.wallet import Wallet


class FakePane:
    """Stands in for HomeView -- just enough for fill_home: a HomeData holder
    + a notify() counter."""
    def __init__(self):
        from webbee.home_view import HomeData
        self.data = HomeData()
        self.notified = 0
    def notify(self):
        self.notified += 1


def _home_slot(workspace="/ws"):
    return SessionSlot(kind="home", workspace=workspace, label="Home",
                       pane=FakePane(), sink=None, agent=None)


def _session_slot(workspace="/ws", label="ws", session_id=""):
    from types import SimpleNamespace
    return SessionSlot(kind="session", workspace=workspace, label=label,
                       pane=FakePane(), sink=None,
                       agent=SimpleNamespace(session_id=session_id))


class _Cfg:
    api_url = "https://auth.imperal.io"
    panel_url = "https://panel.imperal.io"
    intel_enabled = True


async def _tok():
    return "tok"


class _FakeSessions:
    def __init__(self, listing):
        self._listing = listing
    async def list_sessions(self, cfg, token_provider):
        return self._listing


# ---- _mask_email ------------------------------------------------------------

def test_mask_email_normal():
    assert _mask_email("valentin@webhostmost.com") == "v•••@w•••"


def test_mask_email_no_at():
    assert _mask_email("not-an-email") == "n•••"


def test_mask_email_empty():
    assert _mask_email("") == ""
    assert _mask_email(None) == ""


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


# ---- _notify_state_from -----------------------------------------------------

def test_notify_state_from_maps_mirror():
    assert _notify_state_from({}) == "off"
    assert _notify_state_from({"enabled": False}) == "off"
    assert _notify_state_from({"enabled": True, "mirror": ["telegram"]}) == "tg"
    assert _notify_state_from({"enabled": True, "mirror": ["panel"]}) == "panel"
    assert _notify_state_from({"enabled": True, "mirror": ["telegram", "panel"]}) == "both"


# ---- fill_home --------------------------------------------------------------

def test_fill_home_populates_account_wallet_and_meta():
    async def acct(cfg, tp):
        return Account(signed_in=True, nickname="v", plan="pro")
    async def wal(cfg, tp):
        return Wallet(balance=100, cap=500, plan="pro", status="active")
    home = _home_slot()
    slots = SlotManager(); slots.add(home); slots.add(_session_slot(label="myrepo"))
    slots.active_idx = 1
    asyncio.run(fill_home(home, cfg=_Cfg(), token_provider=_tok, slots=slots,
                          account_fetcher=acct, sessions_client=_FakeSessions([]),
                          resources=WorkspaceResources(), version="1.2.3", wallet_fetcher=wal))
    d = home.pane.data
    assert d.account.nickname == "v"
    assert d.wallet.balance == 100
    assert d.version == "1.2.3"
    assert d.endpoint == "https://auth.imperal.io"
    assert home.pane.notified > 0
    assert home._last_fill > 0.0 and home._filling is False


def test_fill_home_raising_account_fetcher_still_sets_other_fields():
    async def raising(cfg, tp):
        raise RuntimeError("boom")
    async def wal(cfg, tp):
        return Wallet(balance=7)
    home = _home_slot()
    slots = SlotManager(); slots.add(home)
    asyncio.run(fill_home(home, cfg=_Cfg(), token_provider=_tok, slots=slots,
                          account_fetcher=raising, sessions_client=_FakeSessions([]),
                          resources=WorkspaceResources(), version="9.9.9", wallet_fetcher=wal))
    d = home.pane.data
    assert d.account is None
    assert d.wallet.balance == 7          # wallet leg survived the account leg raising
    assert d.version == "9.9.9"
    assert home._last_fill > 0.0


def test_fill_home_builds_device_rows_without_pii():
    async def acct(cfg, tp):
        return Account(signed_in=False)
    listing = [{"device": "MacBook", "current": True},
               {"user_agent": "webbee-cli", "ip": "1.2.3.4"}]
    home = _home_slot()
    slots = SlotManager(); slots.add(home)
    asyncio.run(fill_home(home, cfg=_Cfg(), token_provider=_tok, slots=slots,
                          account_fetcher=acct, sessions_client=_FakeSessions(listing),
                          resources=WorkspaceResources(), version="1.0.0", wallet_fetcher=None))
    labels = [r.label for r in home.pane.data.devices]
    assert labels == ["MacBook", "webbee-cli"]     # non-PII fields; raw IP never surfaced
    assert home.pane.data.devices[0].current is True


def test_fill_home_reentrancy_guard():
    calls = []
    async def counting(cfg, tp):
        calls.append(1); await asyncio.sleep(0); return Account(signed_in=False)
    home = _home_slot(); slots = SlotManager(); slots.add(home)
    async def scenario():
        t1 = asyncio.ensure_future(fill_home(home, cfg=_Cfg(), token_provider=_tok, slots=slots,
                                             account_fetcher=counting, sessions_client=_FakeSessions([]),
                                             resources=WorkspaceResources(), version="1", wallet_fetcher=None))
        t2 = asyncio.ensure_future(fill_home(home, cfg=_Cfg(), token_provider=_tok, slots=slots,
                                             account_fetcher=counting, sessions_client=_FakeSessions([]),
                                             resources=WorkspaceResources(), version="1", wallet_fetcher=None))
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


def test_home_input_dock_path_echoes_exactly_once():
    """Regression pin (0.3.24, Valentin live on 0.3.22/0.3.23): typing on Home
    opened a new session tab whose transcript showed the first message TWICE.
    Root cause was `_home_input` echoing the typed line itself AND handing off
    to `ui_hooks["start_turn_in"]` (tui's `_start_turn_in` -> `_run_turn` ->
    `on_line` -> repl's `_handle`), which echoes AGAIN for every non-command
    typed line (`_handle`'s own `slot.sink.user_echo(line)` -- the canonical
    echo every OTHER typed line in the dock already goes through). This fake
    `start_turn_in` mirrors that real seam (it echoes, exactly like `_handle`
    does) so the count below actually exercises the double-echo path, not
    just the echo-free fallback branch `test_home_input_new_slot_sink_records_
    echo` above already covers."""
    slots = SlotManager()
    slots.add(_home_slot(workspace="/cwd"))

    async def fake_make_session_slot(cfg, tp, ws, mode, *, resources, shared_client,
                                      agent_factory, intel_factory, shadow_factory, first):
        s = _session_slot(workspace=ws)
        s.sink = _EchoSink()
        return s

    import webbee.repl as repl_mod
    orig = repl_mod._make_session_slot
    repl_mod._make_session_slot = fake_make_session_slot

    def fake_start_turn_in(slot, text):
        # Stands in for tui._start_turn_in's REAL chain (on_line -> _handle),
        # which echoes the line itself for every non-command typed line.
        slot.sink.user_echo(text)

    async def never_called(slot, text):
        raise AssertionError("fallback run_turn must not fire when start_turn_in is wired")

    try:
        asyncio.run(_home_input(
            "hello", slots=slots, cfg=None, token_provider=None, mode="default",
            resources=WorkspaceResources(), shared_client=None, agent_factory=None,
            intel_factory=None, shadow_factory=None, workspace="/cwd",
            ui_hooks={"start_turn_in": fake_start_turn_in}, run_turn=never_called))
    finally:
        repl_mod._make_session_slot = orig

    new_slot = slots.slots[1]
    assert new_slot.sink.echoed == ["hello"]   # exactly ONE echo -- not two


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
