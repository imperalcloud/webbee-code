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

from rich.align import Align
from rich.text import Text

from webbee.banner_art import WEBBEE_CODE
from webbee.render import _ACCENT, _BEE

HOME_HINT = "type a task — a new tab opens · Ctrl+T here anytime · Alt+N switch"
_SECTIONS = ("Identity", "Tabs", "Repo", "System")


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


def _render_header(console) -> None:
    console.print()
    console.print(Align.center(Text(WEBBEE_CODE, style=f"bold {_BEE}")))
    console.print(Align.center(Text("◆ Home", style=f"bold {_ACCENT}")))
    console.print()


def _render_hint(console) -> None:
    console.print()
    console.print(Align.center(Text(HOME_HINT, style="dim")))
    console.print()


def render_skeleton(console, width: int) -> None:
    """The instant, data-free shell: the logo, `◆ Home`, the four section
    headers (each a dim placeholder line) and the hint. ZERO awaits — called
    synchronously the moment `fill_home` starts, so switching to Home (or
    booting) never shows a blank pane while the async tiles below are still
    in flight. `width` mirrors the other renderers' signatures for parity;
    `Align.center` itself measures the console's LIVE width at print time
    (W2 replay-safe, same vocabulary as `render.RichSink.welcome`), so this
    function has no direct use for it."""
    _render_header(console)
    for name in _SECTIONS:
        console.print(Text(f"  {name}", style=f"bold {_BEE}"))
        console.print(Text("  …", style="dim"))
        console.print()
    _render_hint(console)


def render_identity(console, account) -> None:
    """Nickname prominent, plan next, email masked last — the raw address
    (PII) never prints. `account is None` (not signed in, or the fetch is
    still pending / failed) renders the same neutral placeholder either
    way — Home can't and shouldn't distinguish "loading" from "offline" for
    the person looking at it."""
    console.print(Text("  Identity", style=f"bold {_BEE}"))
    if account is None or not getattr(account, "signed_in", False):
        console.print(Text("  …", style="dim"))
        console.print()
        return
    nickname = getattr(account, "nickname", "") or ""
    headline = f"@{nickname}" if nickname else "(no nickname set)"
    console.print(Text(f"  {headline}", style="bold white"))
    plan = getattr(account, "plan", "") or ""
    if plan:
        status = getattr(account, "plan_status", "") or ""
        tag = f" ({status})" if status and status != "active" else ""
        console.print(Text(f"  {plan} plan{tag}", style="dim"))
    email = getattr(account, "email", "") or ""
    if email:
        console.print(Text(f"  {_mask_email(email)}", style="dim"))
    console.print()


def render_slots_tile(console, slots) -> None:
    """Every OPEN session tab (Home never lists itself), live status glyphs
    (`slot.status_glyph()`) and the always-true hint for how to get one
    started. `slots` is a `SlotManager` — read live, never a snapshot, so
    this always reflects the tabs that exist right now."""
    console.print(Text("  Tabs", style=f"bold {_BEE}"))
    sessions = [(i, s) for i, s in enumerate(slots.slots) if s.kind == "session"]
    if not sessions:
        console.print(Text("  no tabs open yet", style="dim"))
    else:
        active_idx = slots.active_idx
        for idx, s in sessions:
            marker = "●" if idx == active_idx else "○"
            console.print(Text(f"  {marker} {idx} {s.label} {s.status_glyph()}", style="white"))
    console.print(Text("  + Ctrl+T / type to start", style="dim"))
    console.print()


def render_repo_tile(console, profile, branch: str, checkpoints: str | None) -> None:
    """Repo intelligence for whichever workspace `fill_home` picked (the
    active session tab, or the most recently opened one). `profile` falsy
    covers "no repo yet", "intel disabled" and "the fetch failed" alike —
    all three render the same neutral placeholder; there is nothing actionable
    to tell the difference between them."""
    console.print(Text("  Repo", style=f"bold {_BEE}"))
    if not profile:
        console.print(Text("  …", style="dim"))
        console.print()
        return
    langs = ", ".join(sorted((profile.get("languages") or {}).keys())) or "—"
    files = profile.get("file_count", 0)
    console.print(Text(f"  branch {branch or '-'} · {files} files · {langs}", style="white"))
    if checkpoints:
        head = next((ln for ln in checkpoints.splitlines() if ln.strip()), "")
        if head:
            console.print(Text(f"  {head}", style="dim"))
    console.print()


def render_system_tile(console, *, remote_desc: str | None, update_notice: str | None) -> None:
    """Remote-control status (only meaningful once a coding turn has run at
    least once — `fill_home` passes `None` otherwise, a different state from
    "the fetch failed" but rendered the same, since neither is actionable
    from Home) plus a one-line update notice, when there is one."""
    console.print(Text("  System", style=f"bold {_BEE}"))
    line = remote_desc if remote_desc else "remote control: no live session yet"
    console.print(Text(f"  {line}", style="dim"))
    if update_notice:
        console.print(Text(f"  {update_notice}", style="dim"))
    console.print()


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


async def fill_home(slot, *, cfg, token_provider, slots, account_fetcher,
                    sessions_client, resources, version: str) -> None:
    """Paint Home's dashboard: the instant skeleton, then each tile
    best-effort, in order (identity, tabs, repo, system) — a full repaint
    after every step so real data replaces the "…" placeholder as it lands
    (append-only console — see the module docstring, no live-editing one
    region). Self-guarded against overlap (`slot._filling`): a stale
    switch-to-Home while a boot fill is still running is a safe no-op, never
    a second concurrent fetch. `slot._last_fill` (monotonic) is stamped on
    the way out regardless of how it went, so a failed run still counts as
    "tried just now" rather than retrying on every single switch.
    `sessions_client` is accepted for interface/signature parity with the
    wiring map's reachable-data-sources list (§5) but unused here — the
    multi-surface "other devices" list is a different concept from this
    tile's OWN dock tabs; a wallet/activity/connections tile is explicitly
    W5 (map §5: no client wrappers exist for those yet)."""
    if slot._filling:
        return
    slot._filling = True
    console = slot.pane.console
    width = console.width

    account = None
    profile, branch, checkpoints = None, "-", None
    remote_desc, update_notice = None, None

    def _repaint() -> None:
        try:
            console.clear()
            render_skeleton(console, width)
        except Exception:
            pass
        for render_fn, args, kwargs in (
            (render_identity, (account,), {}),
            (render_slots_tile, (slots,), {}),
            (render_repo_tile, (profile, branch, checkpoints), {}),
            (render_system_tile, (), {"remote_desc": remote_desc, "update_notice": update_notice}),
        ):
            try:
                render_fn(console, *args, **kwargs)
            except Exception:
                pass
        try:
            slot.pane.notify()
        except Exception:
            pass

    try:
        _repaint()   # instant shell — no data yet

        try:
            account = await account_fetcher(cfg, token_provider)
        except Exception:
            account = None
        _repaint()

        try:
            picked = _pick_session_slot(slots)
            ws = picked.workspace if picked is not None else slot.workspace
            bundle = resources.get(ws) or {}
            branch = bundle.get("git_branch", "-")
            intel = bundle.get("intel")
            shadow = bundle.get("shadow")
            if intel is not None:
                try:
                    profile = intel.repo_profile()
                except Exception:
                    profile = None
            if shadow is not None:
                try:
                    checkpoints = await asyncio.to_thread(shadow.describe)
                except Exception:
                    checkpoints = None
        except Exception:
            picked = None
        _repaint()

        try:
            sid = getattr(getattr(picked, "agent", None), "session_id", "") if picked is not None else ""
            if sid:
                from webbee import remote as _remote
                try:
                    state = await _remote.get_remote(cfg, token_provider, sid)
                    remote_desc = _remote.describe(state)
                except Exception:
                    remote_desc = None
        except Exception:
            remote_desc = None
        try:
            from pathlib import Path

            from webbee.update import check_for_update, default_fetch
            cache = Path(os.path.expanduser("~/.cache/webbee/update.json"))
            update_notice = await asyncio.to_thread(
                check_for_update, version, cache_path=cache, now=time.time(), fetch=default_fetch)
        except Exception:
            update_notice = None
        _repaint()
    finally:
        slot._last_fill = time.monotonic()
        slot._filling = False
