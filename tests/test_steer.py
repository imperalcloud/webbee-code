"""Idle-steer pickup (terminal liveness v2 §B): the background poller that
drains queued Telegram/panel instructions while the REPL sits idle at the
prompt and runs them through the normal turn path. Unit level -- the repl
wiring is covered in test_repl.py; the fetcher in test_thread.py."""
import asyncio
import contextlib

import webbee.steer as SP


class _Cfg:
    api_url = "http://x"


async def _tp():
    return "tok"


class _FakeImperalClient:
    """House pattern (test_session.py/test_repl.py) for the whoami call."""
    def __init__(self, cfg, token_provider):
        pass

    async def whoami(self):
        return "user-1"


def _patch_identity(monkeypatch):
    import imperal_mcp.client as ic

    import webbee.repo as R
    monkeypatch.setattr(ic, "ImperalClient", _FakeImperalClient)
    monkeypatch.setattr(R, "find_repo_root", lambda start: "/repo")
    monkeypatch.setattr(R, "compute_repo_key", lambda root: "ab12cd34ef56")


def _drive(poller_coro, until, timeout=2.0):
    """Run the poller as a background task until `until()` (or timeout), then
    cancel + drain it -- mirrors the repl lifecycle (boot-start, exit-cancel)."""
    async def main():
        task = asyncio.ensure_future(poller_coro)
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while not until() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    asyncio.run(main())


# ── session-id derivation ─────────────────────────────────────────────────────

def test_derive_session_id_is_real_repo_session_not_boot_placeholder(monkeypatch):
    # The gateway keys coding sessions stable-per-user+repo:
    # marathon-{imperal_id}-r{repo_key} (agent_sessions/router.py builds it from
    # the repo_key the CLIENT sends in coding_context). The boot-replay
    # placeholder (-rboot) passes the ownership prefix check but MISSES the
    # per-session remote-control state key (resolve_state reads
    # k_state(session_id)), so pending-steer would never fire with it.
    _patch_identity(monkeypatch)
    sid = asyncio.run(SP.derive_session_id(_Cfg(), _tp, "/repo/sub"))
    assert sid == "marathon-user-1-rab12cd34ef56"
    assert not sid.endswith("-rboot")


def test_derive_session_id_coding_prefix_for_once_mode(monkeypatch):
    # --once turns POST marathon=False -> the gateway prefixes "coding-".
    _patch_identity(monkeypatch)
    sid = asyncio.run(SP.derive_session_id(_Cfg(), _tp, "/repo", marathon=False))
    assert sid == "coding-user-1-rab12cd34ef56"


def test_derive_session_id_appends_slot_suffix_when_given(monkeypatch):
    # W4b T5: a later tab's poller derives ITS OWN id -- the SAME -s{slot}
    # suffix the gateway mints server-side from StartRequest.slot.
    _patch_identity(monkeypatch)
    sid = asyncio.run(SP.derive_session_id(_Cfg(), _tp, "/repo", slot_id="ab12cd"))
    assert sid == "marathon-user-1-rab12cd34ef56-sab12cd"


def test_derive_session_id_omits_slot_suffix_when_empty(monkeypatch):
    # Legacy/tab-1 contract: slot_id="" (the default) keeps today's id
    # byte-identical -- no trailing "-s" of any kind.
    _patch_identity(monkeypatch)
    sid = asyncio.run(SP.derive_session_id(_Cfg(), _tp, "/repo"))
    assert sid == "marathon-user-1-rab12cd34ef56"


# ── poll loop ─────────────────────────────────────────────────────────────────

def test_poll_submits_first_item_with_surface(monkeypatch):
    polled = []
    submitted = []

    async def fake_fetch(cfg, tp, session_id):
        polled.append(session_id)
        return {"items": [{"text": "push the fix", "surface": "telegram", "ts": 1}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface))

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-user-1-rlive",
                              interval=0.01),
           until=lambda: submitted)
    assert submitted[0] == ("push the fix", "telegram")
    # a live agent session id wins over derivation (it IS the gateway truth)
    assert polled[0] == "marathon-user-1-rlive"


def test_poll_passes_item_iid_to_submit(monkeypatch):
    # steer-iid-dedup pickup path: /pending-steer items carry the queue entry's
    # `iid`; the poller must hand it to submit so the turn POST carries
    # steer_iid and the kernel's dedup ring can drop an at-least-once twin.
    submitted = []

    async def fake_fetch(cfg, tp, session_id):
        return {"items": [{"text": "push the fix", "surface": "telegram", "ts": 1,
                           "iid": "iid-42"}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface, steer_iid))

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: submitted)
    assert submitted[0] == ("push the fix", "telegram", "iid-42")


def test_poll_item_without_iid_submits_empty_iid(monkeypatch):
    # An older gateway's items carry no `iid` -- submit gets "" (the turn POST
    # then omits steer_iid entirely), never a crash.
    submitted = []

    async def fake_fetch(cfg, tp, session_id):
        return {"items": [{"text": "resume", "surface": "telegram", "ts": 2}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface, steer_iid))

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: submitted)
    assert submitted[0] == ("resume", "telegram", "")


def test_poll_no_items_never_submits(monkeypatch):
    polled = []
    submitted = []

    async def fake_fetch(cfg, tp, session_id):
        polled.append(session_id)
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface))

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: len(polled) >= 3)
    assert len(polled) >= 3          # kept polling
    assert submitted == []           # nothing happened


def test_poll_silent_on_network_error_and_recovers(monkeypatch):
    calls = []
    submitted = []

    async def flaky_fetch(cfg, tp, session_id):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("connection refused")
        return {"items": [{"text": "resume", "surface": "telegram", "ts": 2}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", flaky_fetch)

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface))

    # No exception escapes the poller; the tick after the blip succeeds.
    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: submitted)
    assert submitted == [("resume", "telegram")]
    assert len(calls) >= 3


def test_poll_never_fetches_while_busy(monkeypatch):
    fetched = []
    submitted = []
    busy = {"on": True}

    async def fake_fetch(cfg, tp, session_id):
        fetched.append(1)
        return {"items": [{"text": "steer", "surface": "telegram", "ts": 3}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface))

    ticks = {"n": 0}

    def is_busy():
        ticks["n"] += 1
        if ticks["n"] >= 5:      # a running turn ends after a few ticks
            busy["on"] = False
        return busy["on"]

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=is_busy,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: submitted)
    # During the busy ticks (1..4) the gateway was NEVER polled -- the drain is
    # destructive, so polling mid-turn would strand items client-side.
    assert fetched != [] and submitted == [("steer", "telegram")]
    assert ticks["n"] >= 5


def test_multi_item_drain_runs_in_order_one_per_tick_nothing_lost(monkeypatch):
    fetches = []
    submitted = []

    async def fake_fetch(cfg, tp, session_id):
        fetches.append(1)
        if len(fetches) == 1:
            return {"items": [{"text": "first", "surface": "telegram", "ts": 1},
                              {"text": "second", "surface": "panel", "ts": 2},
                              {"text": "third", "surface": "telegram", "ts": 3}]}
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface))

    # The gateway drain returns each item exactly ONCE -- items 2..n of a batch
    # must be buffered locally and run on the FOLLOWING idle ticks, in order.
    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: len(submitted) >= 3)
    assert submitted == [("first", "telegram"), ("second", "panel"),
                         ("third", "telegram")]


def test_item_deferred_not_lost_when_turn_starts_mid_tick(monkeypatch):
    # is_busy is re-checked right before submit: a locally-typed line can win
    # the race between the fetch and the submit; the drained item must go back
    # to the local backlog (drain is destructive -- dropping it loses it).
    submitted = []
    busy_answers = [False, True, False, False]   # gate, pre-submit(busy!), gate, pre-submit

    async def fake_fetch(cfg, tp, session_id):
        return {"items": [{"text": "later", "surface": "telegram", "ts": 4}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    def is_busy():
        return busy_answers.pop(0) if busy_answers else False

    async def submit(text, surface, steer_iid=""):
        submitted.append((text, surface))

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=is_busy,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: submitted)
    assert submitted[0] == ("later", "telegram")


def test_poller_exits_when_submitted_turn_absorbs_the_cancel(monkeypatch):
    # Exit hygiene: /exit while a picked-up turn is in flight cancels the
    # poller task, but the turn path (repl._run_turn) treats CancelledError as
    # a user interrupt and swallows it. The poller must notice the absorbed
    # cancel and stop -- otherwise asyncio.run's shutdown hangs on a task that
    # ignored cancellation.
    in_submit = asyncio.Event()

    async def fake_fetch(cfg, tp, session_id):
        return {"items": [{"text": "long job", "surface": "telegram", "ts": 5}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def swallowing_submit(text, surface, steer_iid=""):
        in_submit.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass                     # mirrors _run_turn's Interrupted handling

    async def main():
        task = asyncio.ensure_future(SP.poll_idle_steer(
            _Cfg(), _tp, workspace=".", is_busy=lambda: False,
            submit=swallowing_submit, live_session_id=lambda: "marathon-u-r1",
            interval=0.01))
        await asyncio.wait_for(in_submit.wait(), timeout=2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)   # must END, not loop on
        assert task.done()

    asyncio.run(main())


# ── requested_mode → on_mode seam (full-queue-layer mode adoption) ────────────
# The SAME pending-steer fetch carries the gateway's one-shot requested_mode
# (GETDEL server-side). The poller hands it to the injected on_mode(mode,
# surface) seam BEFORE any fetched item submits (the flip governs the turn it
# rode in with) and stays alive whatever the seam or the payload does.

def test_requested_mode_handed_to_on_mode_before_items_submit(monkeypatch):
    events = []

    async def fake_fetch(cfg, tp, session_id):
        return {"items": [{"text": "do it", "surface": "telegram", "ts": 1}],
                "requested_mode": {"mode": "plan", "surface": "telegram"}}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        events.append(("submit", text))

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              on_mode=lambda m, s: events.append(("mode", m, s)),
                              interval=0.01),
           until=lambda: ("submit", "do it") in events)
    assert events[0] == ("mode", "plan", "telegram")   # mode first, then the turn
    assert events[1] == ("submit", "do it")


def test_requested_mode_absent_or_malformed_never_calls_on_mode(monkeypatch):
    fetches = []
    modes = []
    payloads = [{"items": []},                                   # old gateway shape
                {"items": [], "requested_mode": None},           # explicit null
                {"items": [], "requested_mode": "autopilot"},    # not a dict
                {"items": [], "requested_mode": {"surface": "telegram"}},  # no mode
                {"items": [{"text": "go", "surface": "telegram", "ts": 1}]}]

    async def fake_fetch(cfg, tp, session_id):
        fetches.append(1)
        return payloads[min(len(fetches), len(payloads)) - 1]

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)
    submitted = []

    async def submit(text, surface, steer_iid=""):
        submitted.append(text)

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              on_mode=lambda m, s: modes.append((m, s)),
                              interval=0.01),
           until=lambda: submitted)
    assert modes == []                                 # nothing well-formed arrived
    assert submitted == ["go"]                         # items still flow


def test_on_mode_error_never_kills_the_poller(monkeypatch):
    fetches = []

    async def fake_fetch(cfg, tp, session_id):
        fetches.append(1)
        if len(fetches) == 1:
            return {"items": [], "requested_mode": {"mode": "plan", "surface": "telegram"}}
        return {"items": [{"text": "still alive", "surface": "telegram", "ts": 2}]}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)
    submitted = []

    async def submit(text, surface, steer_iid=""):
        submitted.append(text)

    def bad_on_mode(mode, surface):
        raise RuntimeError("ui bug")

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              on_mode=bad_on_mode, interval=0.01),
           until=lambda: submitted)
    assert submitted == ["still alive"]                # the seam error cost one tick, not the poller


def test_requested_mode_without_on_mode_seam_is_ignored(monkeypatch):
    # Old wiring (no on_mode kwarg) against a new gateway: the request is
    # dropped silently — items keep flowing exactly as before.
    async def fake_fetch(cfg, tp, session_id):
        return {"items": [{"text": "go", "surface": "telegram", "ts": 1}],
                "requested_mode": {"mode": "autopilot", "surface": "telegram"}}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)
    submitted = []

    async def submit(text, surface, steer_iid=""):
        submitted.append(text)

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: submitted)
    assert submitted == ["go"]


# ── adaptive idle cadence (Task 12: 4s→30s after 5 quiet minutes) ────────────
# poll_idle_steer's sleep is the FIRST statement of the loop, so a fake
# asyncio.sleep both records the requested duration and drives a fake
# monotonic clock -- no real waiting, fully deterministic.

def test_adaptive_interval_relaxes_after_idle_and_resets_on_activity(monkeypatch):
    """Drive poll_idle_steer with a fake clock/sleep recorder: first ticks use
    4s; after 300 fake-idle seconds the recorded sleep is 30s; a submitted
    item resets the next sleep to 4s."""
    sleeps = []
    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            clock["t"] = 301.0   # simulate 300+s of wall-clock idle between ticks
        if len(sleeps) >= 5:
            raise asyncio.CancelledError()

    monkeypatch.setattr(SP.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    async def fake_fetch(cfg, tp, session_id):
        calls["n"] += 1
        if calls["n"] == 4:
            return {"items": [{"text": "resume", "surface": "telegram", "ts": 1}]}
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        pass

    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(SP.poll_idle_steer(
            _Cfg(), _tp, workspace=".", is_busy=lambda: False, submit=submit,
            live_session_id=lambda: "marathon-u-r1", _monotonic=fake_monotonic))

    assert sleeps[:2] == [4.0, 4.0]      # idle-fresh cadence (_POLL_INTERVAL default)
    assert sleeps[2:4] == [30.0, 30.0]   # relaxed after 300 idle seconds
    assert sleeps[4] == 4.0              # a submitted item resets the cadence


# ── applied-mode report (T6.2): mode_getter → fetch_pending_steer(mode=) ─────

def test_mode_getter_threads_mode_into_fetch(monkeypatch):
    seen_modes = []

    async def fake_fetch(cfg, tp, session_id, **kw):
        seen_modes.append(kw.get("mode"))
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        pass

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              mode_getter=lambda: "plan", interval=0.01),
           until=lambda: len(seen_modes) >= 2)
    assert seen_modes[0] == "plan"


def test_mode_getter_absent_never_passes_mode_kwarg(monkeypatch):
    # Old-style test doubles (and any real gateway call before this feature)
    # accept no `mode` kwarg at all -- omitted entirely with no mode_getter,
    # exactly like `client` when the repl doesn't own one.
    calls = {"n": 0}

    async def fake_fetch(cfg, tp, session_id):   # no **kw -- would TypeError if a kwarg leaked
        calls["n"] += 1
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        pass

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              interval=0.01),
           until=lambda: calls["n"] >= 2)
    assert calls["n"] >= 2   # ran clean -- no TypeError from a stray kwarg


def test_mode_getter_returning_empty_string_omits_mode_kwarg(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(cfg, tp, session_id):   # no **kw
        calls["n"] += 1
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        pass

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              mode_getter=lambda: "", interval=0.01),
           until=lambda: calls["n"] >= 2)
    assert calls["n"] >= 2


def test_mode_getter_read_fresh_every_tick_no_extra_poke_needed(monkeypatch):
    # T6.2 contract: a LOCAL mode change (Shift-Tab, /mode, a remote flip)
    # needs no immediate poke -- mode_getter is called fresh each tick, so
    # the very next poll (within one `interval`) reports the new value.
    current = {"mode": "default"}
    seen_modes = []

    async def fake_fetch(cfg, tp, session_id, **kw):
        seen_modes.append(kw.get("mode"))
        if len(seen_modes) == 2:
            current["mode"] = "autopilot"   # a mode change lands between ticks
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface, steer_iid=""):
        pass

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-u-r1",
                              mode_getter=lambda: current["mode"], interval=0.01),
           until=lambda: len(seen_modes) >= 3)
    assert seen_modes[:2] == ["default", "default"]
    assert seen_modes[2] == "autopilot"   # the very next tick already reports it


# ── per-slot stagger + slot_id threading (W4b T5) ────────────────────────────

def test_initial_delay_sleeps_once_before_the_first_tick(monkeypatch):
    # The repl staggers each new per-slot poller's start so several tabs
    # opened back-to-back don't all hit the gateway in the same instant --
    # `initial_delay` is slept EXACTLY once, before the loop's own cadence
    # sleep ever runs.
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(SP.asyncio, "sleep", fake_sleep)

    async def submit(text, surface, steer_iid=""):
        pass

    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(SP.poll_idle_steer(
            _Cfg(), _tp, workspace=".", is_busy=lambda: False, submit=submit,
            live_session_id=lambda: "marathon-u-r1", initial_delay=2.0))

    assert sleeps[0] == 2.0                # the stagger, first
    assert sleeps[1] == SP._POLL_INTERVAL  # then the ordinary fast-cadence tick


def test_initial_delay_zero_never_sleeps_extra(monkeypatch):
    # Default (0.0, every existing caller) must stay byte-identical -- no
    # extra asyncio.sleep(0.0) call at all.
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(SP.asyncio, "sleep", fake_sleep)

    async def submit(text, surface, steer_iid=""):
        pass

    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(SP.poll_idle_steer(
            _Cfg(), _tp, workspace=".", is_busy=lambda: False, submit=submit,
            live_session_id=lambda: "marathon-u-r1"))

    assert sleeps == [SP._POLL_INTERVAL]   # ONE sleep total, the ordinary tick


def test_slot_id_threaded_into_derivation_when_no_live_session_yet(monkeypatch):
    polled = []

    async def fake_fetch(cfg, tp, session_id):
        polled.append(session_id)
        return {"items": []}

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)
    _patch_identity(monkeypatch)

    async def submit(text, surface, steer_iid=""):
        pass

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace="/repo", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "",
                              slot_id="ab12cd", interval=0.01),
           until=lambda: polled)
    assert polled[0] == "marathon-user-1-rab12cd34ef56-sab12cd"
