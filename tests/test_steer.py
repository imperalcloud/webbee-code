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


# ── poll loop ─────────────────────────────────────────────────────────────────

def test_poll_submits_first_item_with_surface(monkeypatch):
    polled = []
    submitted = []

    async def fake_fetch(cfg, tp, session_id):
        polled.append(session_id)
        return [{"text": "push the fix", "surface": "telegram", "ts": 1}]

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface):
        submitted.append((text, surface))

    _drive(SP.poll_idle_steer(_Cfg(), _tp, workspace=".", is_busy=lambda: False,
                              submit=submit, live_session_id=lambda: "marathon-user-1-rlive",
                              interval=0.01),
           until=lambda: submitted)
    assert submitted[0] == ("push the fix", "telegram")
    # a live agent session id wins over derivation (it IS the gateway truth)
    assert polled[0] == "marathon-user-1-rlive"


def test_poll_no_items_never_submits(monkeypatch):
    polled = []
    submitted = []

    async def fake_fetch(cfg, tp, session_id):
        polled.append(session_id)
        return []

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface):
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
        return [{"text": "resume", "surface": "telegram", "ts": 2}]

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", flaky_fetch)

    async def submit(text, surface):
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
        return [{"text": "steer", "surface": "telegram", "ts": 3}]

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface):
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
            return [{"text": "first", "surface": "telegram", "ts": 1},
                    {"text": "second", "surface": "panel", "ts": 2},
                    {"text": "third", "surface": "telegram", "ts": 3}]
        return []

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def submit(text, surface):
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
        return [{"text": "later", "surface": "telegram", "ts": 4}]

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    def is_busy():
        return busy_answers.pop(0) if busy_answers else False

    async def submit(text, surface):
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
        return [{"text": "long job", "surface": "telegram", "ts": 5}]

    import webbee.thread as TH
    monkeypatch.setattr(TH, "fetch_pending_steer", fake_fetch)

    async def swallowing_submit(text, surface):
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
