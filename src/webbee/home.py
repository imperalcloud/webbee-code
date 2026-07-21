"""The Home tab — the dock's new-tab page (W4a Task 6). Slot 0 is always
Home (`SessionSlot(kind="home", sink=None, agent=None)`, wiring map §1): its
own pane holds a live, best-effort DASHBOARD instead of a session transcript
— identity, open tabs, repo intelligence, system state — filled
asynchronously by `fill_home` so switching to Home (or booting) never blocks
on a network call. Every print goes through `slot.pane.console` (the SAME
RecordingConsole every session slot uses — W2 reflow re-centers on resize
for free, no Home-specific sizing code needed). Zero new server logic: every
data source below is an EXISTING reader (account.py / intel / checkpoints /
remote.py / update.py) — this module only arranges what they already return.

Rendering is append-only (Rich prints into a StringIO, never edits a
previous line — see output_pane.py), so "patch tiles as data lands" means
clear() + a full repaint each time a new piece of data arrives, not
in-place editing of one region: `fill_home` clears, paints the instant
`render_skeleton` shell, then re-clears and re-paints the WHOLE dashboard
(skeleton + whatever tiles have real data so far, "…" for the rest) after
each async fetch completes — bounded (four repaints total: the instant
skeleton, then one per async tile) and cheap enough because the pane's own
records ring is what makes a full repaint affordable at all."""
from __future__ import annotations

import asyncio
import os
import time


def _mask_email(email: str) -> str:
    """PII-safe display of an email: only the FIRST character of the local
    part and of the domain survive (`valentin@webhostmost.com` ->
    `v•••@w•••`) — enough to recognize your OWN account at a glance, nothing
    an onlooker could read off a shared screen. Empty input -> empty output;
    a malformed value with no `@` gets the same first-char+mask treatment
    applied to the whole string, so it still can't leak past one character."""
    email = (email or "").strip()
    if not email:
        return ""
    if "@" not in email:
        return f"{email[0]}•••"
    local, _, domain = email.partition("@")
    local_mask = f"{local[0]}•••" if local else "•••"
    domain_mask = f"{domain[0]}•••" if domain else "•••"
    return f"{local_mask}@{domain_mask}"


def is_stale(slot, now: float, ttl: float = 300.0) -> bool:
    """PURE. The switch-to-Home refill gate: a slot that has never been
    filled (`_last_fill` at its `0.0` default) is ALWAYS stale, regardless
    of how small `now` itself is (e.g. moments after process start, when
    `time.monotonic()` hasn't yet ticked past `ttl`) — "never filled" and
    "filled too long ago" both mean the same thing here: go fill it."""
    last = getattr(slot, "_last_fill", 0.0)
    if last <= 0.0:
        return True
    return (now - last) > ttl


def _pick_session_slot(slots):
    """Which session tab Home's repo/system tiles describe: the ACTIVE slot
    when it's itself a real session (the tab you're actually looking at is
    the most relevant one — true right after boot, when Home's own fill runs
    while the first session tab is active); otherwise the most recently
    OPENED session tab; `None` when no session tab exists at all (every tab
    closed — the kernel run itself survives regardless, browser-tab model)."""
    active = slots.active()
    if active.kind == "session":
        return active
    for s in reversed(slots.slots):
        if s.kind == "session":
            return s
    return None


def _notify_state_from(state: dict) -> str:
    """PURE. Collapse a remote-control state dict (remote.get_remote) into the
    Home notifications segmented value: off | panel | tg | both."""
    if not state or not state.get("enabled"):
        return "off"
    mirror = set(state.get("mirror", []) or [])
    tg, panel = "telegram" in mirror, "panel" in mirror
    if tg and panel:
        return "both"
    if tg:
        return "tg"
    if panel:
        return "panel"
    return "off"


def _device_label(s: dict) -> str:
    """A non-PII human label for a device/session row. The `/v1/auth/sessions`
    payload names the human label `label`/`surface` (the SAME keys
    render.sessions_table and the `/sessions revoke` path read); the raw IP
    lives in a separate field we never read here. `label`/`surface` first,
    then harmless fallbacks, then a neutral default."""
    for k in ("label", "surface", "device", "name", "client"):
        v = s.get(k)
        if v:
            return str(v)
    return "session"


async def fill_home(slot, *, cfg, token_provider, slots, account_fetcher,
                    sessions_client, resources, version, wallet_fetcher=None) -> None:
    """Populate the Home dashboard's HomeData, best-effort, staged (a repaint
    after each leg so real data replaces the placeholder as it lands). The
    slot's pane is a HomeView; we mutate `pane.data` and call `pane.notify()`
    (-> app.invalidate()), never re-run rendering ourselves. Self-guarded
    against overlap (`slot._filling`); `slot._last_fill` stamped on the way
    out regardless of outcome. NEVER raises, NEVER blocks boot."""
    if slot._filling:
        return
    slot._filling = True
    from webbee.home_view import HomeData, DeviceRow
    view = slot.pane
    data = getattr(view, "data", None)
    if not isinstance(data, HomeData):
        data = HomeData()

    def _repaint():
        try:
            view.data = data
            view.notify()
        except Exception:
            pass

    try:
        data.version = version
        data.endpoint = getattr(cfg, "api_url", "") or ""
        data.intel_enabled = bool(getattr(cfg, "intel_enabled", True))
        _repaint()

        try:
            data.account = await account_fetcher(cfg, token_provider)
        except Exception:
            data.account = None
        _repaint()

        if wallet_fetcher is not None:
            try:
                data.wallet = await wallet_fetcher(cfg, token_provider)
            except Exception:
                data.wallet = None
            _repaint()

        try:
            data.recent = list(resources.roots())
        except Exception:
            data.recent = []
        try:
            picked = _pick_session_slot(slots)
            ws = picked.workspace if picked is not None else slot.workspace
            bundle = resources.get(ws) or {}
            intel = bundle.get("intel")
            if intel is not None:
                prof = intel.repo_profile()
                data.intel_status = f"{prof.get('file_count', 0)} files indexed"
        except Exception:
            pass
        _repaint()

        try:
            listing = await sessions_client.list_sessions(cfg, token_provider)
            data.devices = [DeviceRow(label=_device_label(s),
                                      current=bool(s.get("current") or s.get("is_current")))
                            for s in (listing or [])]
        except Exception:
            data.devices = []
        _repaint()

        try:
            picked = _pick_session_slot(slots)
            sid = getattr(getattr(picked, "agent", None), "session_id", "") if picked is not None else ""
            if sid:
                from webbee import remote as _remote
                state = await _remote.get_remote(cfg, token_provider, sid)
                data.remote_desc = _remote.describe(state)
                data.notify_state = _notify_state_from(state)
        except Exception:
            pass
        try:
            from pathlib import Path

            from webbee.update import check_for_update, default_fetch
            cache = Path(os.path.expanduser("~/.cache/webbee/update.json"))
            data.update_notice = await asyncio.to_thread(
                check_for_update, version, cache_path=cache, now=time.time(),
                fetch=default_fetch) or ""
        except Exception:
            data.update_notice = ""
        _repaint()
    finally:
        slot._last_fill = time.monotonic()
        slot._filling = False
