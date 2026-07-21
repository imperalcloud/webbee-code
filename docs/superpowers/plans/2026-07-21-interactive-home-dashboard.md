# Interactive Home Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan. Each `### Task N` is a self-contained unit: dispatch it to a fresh subagent, have the subagent follow the TDD steps verbatim (write the failing test, run it, watch it fail, write minimal code, run it, watch it pass, commit), and verify each returned diff before moving on. Do NOT batch tasks; do NOT skip the "run it and watch it fail" step.

**Goal:** Turn Webbee Code's Home tab (slot 0) from a flat append-only text skeleton into an interactive dashboard — "a website inside the terminal": every actionable element clickable by mouse and selectable by keyboard, with hover/focus highlight and per-action hints. Surface identity+plan, credits balance, per-tab session spend, open tabs (mode/close per tab), recent repos (one-click resume), your devices, and a terminal Settings panel (new-tab mode / notifications / top-up). Fix two affordances: the tab-bar "+" becomes bee-yellow/prominent, and Ctrl+T opens a new tab (today it jumps to Home).

**Architecture:** A new `home_view.py` owns the interactive Home. It splits cleanly into (a) a **pure core** — `HomeData`/`Wallet`/`DeviceRow`/`TabRow` snapshots, an `ActionItem`/`HomeModel` actionable-item model with focus/nav/dispatch, and pure builders `tab_rows(slots)` + `build_home_model(data, tabs, actions)` — with **zero prompt_toolkit imports at module top** (mirrors `tabs.py` discipline, unit-testable without an `Application`); and (b) a `HomeView` render component that builds `FormattedText` fragments with per-fragment 3-tuple mouse handlers (mirrors `tabs.py:111-118,68-83`) and exposes `.window` exactly like `OutputPane` does, so the dock's `DynamicContainer(lambda: _pane().window)` swap (`tui.py:1142`) keeps working unchanged. The Home slot's `.pane` **becomes** a `HomeView` (not an `OutputPane`); `HomeView` duck-types the small `OutputPane` surface the ticker/layout touch (`.window`, `.console`, `.reflow`, `.edge_tick`, `.flash`, `.notify`, `.scroll`, `._view_h`, `.forward_mouse`). `home.py` keeps its async fetch orchestration (`fill_home`) but its per-tile renderers are deleted; `fill_home` now populates a `HomeData` on the view and repaints via the view's `notify()` (→ `app.invalidate()`). Interaction never re-runs `fill_home`. New leaf modules: `wallet.py` (credits), `newtab_mode.py` (persisted new-tab default mode), `urlopen.py` (minimal URL opener). tui wiring: yellow `tab.new`, `c-t` → new-tab seam, Home-scoped key bindings (↑↓/Tab/↵/←→) gated on active-slot `kind=="home"`, and `?1003` hover enabled only while Home is active.

**Tech Stack:** Python 3, prompt_toolkit 3.0.52, Rich, httpx (async, bounded timeouts), pytest. Repo `/Users/val-mac/Nextcloud/Projects/webbee/`, source `src/webbee/`, tests `tests/`.

## Global Constraints

- Version bumps to **0.3.26** (`pyproject.toml` + `src/webbee/__init__.py`) in the final task only.
- **English-only UI text.** All product/UI strings in English; run a Cyrillic grep on every shipped file before done.
- **No AI attribution** in commits (no `Co-Authored-By`, no "Generated with"). Commit subjects concise, present-tense.
- **Best-effort fetchers never block boot and never raise** — bounded 3s timeout, `try/except`, return `None`/`[]` → neutral placeholder. `fill_home` runs as a bg task from the moment Home exists.
- **PII masked** — email via existing `home._mask_email`; device rows must never surface a raw IP (prefer non-PII fields; mask on sight). Wallet balance is the user's own, shown as-is.
- **Claims doctrine (Locus of Authority)** governs the Trust/Security tile: every line must be TRUE and defensible; confirm copy against `~/Nextcloud/MCP-Marketing-Imperal/07-voice-and-messaging.md` and the docs URL against `docs.imperal.io` before ship. A concrete default is provided.
- **prompt_toolkit 3.0.52** — per-fragment 3-tuple `mouse_handler` firing on `MouseEventType.MOUSE_UP`, `NotImplemented` otherwise; `MOUSE_MOVE` sets hover. No pt import at the top of `home_view.py` (only inside `HomeView` methods).
- **Mirror the `tabs.py` handler pattern** — 3-tuple fragments, `MOUSE_UP` dispatch, `NotImplemented` fall-through so wheel/scroll keep working.
- **`?1003` scoped to Home only** — enabled on entering Home, restored to `?1002` on leaving; teardown (`configure_mouse_modes._disable`) already clears `?1003l`. Hover itself is LIVE-verified, not headless-testable.
- **User-facing term is "credits"** (internal/API name is tokens). `_fmt_tokens` (render.py) formats both.
- Every touched `.py` file passes `python -m py_compile`. Existing suite stays green.

---

### Task 1: `wallet.py` — credits balance client + `test_wallet.py`

**Files**
- Create `src/webbee/wallet.py`
- Create `tests/test_wallet.py`

**Interfaces**
- Consumes: `cfg.api_url` (str), `token_provider` (async `() -> str`).
- Produces: `Wallet(balance:int, cap:int, plan:str, status:str, included_tokens:int)` (frozen dataclass); `async def fetch_wallet(cfg, token_provider, *, get=None) -> Wallet | None`.

Grounding: mirrors `account.py:34-39,57-93` house client. Gateway: `GET /v1/billing/wallet` (Bearer) → `{balance:int, plan:str, status:str, cap:int, included_tokens:int}` (alias `/v1/billing/balance`). 402 / non-200 / timeout / no-token → `None`.

- [ ] **Step 1: Write the failing test.** Create `tests/test_wallet.py`:
```python
import asyncio

from webbee.wallet import Wallet, fetch_wallet


class _Cfg:
    api_url = "https://auth.imperal.io"


def _run(get):
    async def _tok():
        return "tok"
    return asyncio.run(fetch_wallet(_Cfg(), _tok, get=get))


def test_parses_wallet_response():
    async def get(path):
        assert path == "/v1/billing/wallet"
        return {"balance": 1250, "cap": 5000, "plan": "pro",
                "status": "active", "included_tokens": 2000}
    w = _run(get)
    assert w == Wallet(balance=1250, cap=5000, plan="pro",
                       status="active", included_tokens=2000)


def test_none_on_402():
    async def get(path):
        raise RuntimeError("402 Payment Required")
    assert _run(get) is None


def test_none_on_non_200():
    async def get(path):
        raise RuntimeError("503")
    assert _run(get) is None


def test_none_when_no_token():
    async def boom():
        raise RuntimeError("no creds")
    async def get(path):
        return {"balance": 1}
    assert asyncio.run(fetch_wallet(_Cfg(), boom, get=get)) is None


def test_coerces_missing_and_bad_fields():
    async def get(path):
        return {"balance": "77", "plan": None}
    w = _run(get)
    assert w.balance == 77 and w.cap == 0 and w.plan == "" and w.status == ""
```
- [ ] **Step 2: Run it, watch it fail.** `python -m pytest tests/test_wallet.py -q` → `ModuleNotFoundError: No module named 'webbee.wallet'`.
- [ ] **Step 3: Minimal impl.** Create `src/webbee/wallet.py`:
```python
"""Wallet client — this account's credits balance, cap and plan status, read
from the gateway for Home's Wallet tile. Best-effort, house pattern
(account.py/sessions.py/remote.py): (cfg, token_provider), lazy httpx, Bearer
auth, a bounded 3s timeout, and it NEVER raises -- ANY failure (no token, a
402 Payment Required, a non-200, a timeout, an older gateway without the
route) returns None so the tile shows a neutral placeholder rather than
crashing or blocking Home's boot. User-facing term for `balance` is
"credits"; the internal/API name is tokens."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Wallet:
    balance: int = 0
    cap: int = 0
    plan: str = ""
    status: str = ""
    included_tokens: int = 0


async def _default_get(cfg, token: str, path: str) -> dict:
    import httpx
    async with httpx.AsyncClient(base_url=cfg.api_url, timeout=3.0) as c:
        r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


async def fetch_wallet(cfg, token_provider, *, get=None) -> "Wallet | None":
    """The account's live wallet, or None on ANY failure (see module doc).
    `get=` is a DI seam for tests (mirrors account.fetch_account): an async
    callable `get(path) -> dict` that raises on a non-200/402 -- production
    resolves the token then hits GET /v1/billing/wallet."""
    try:
        token = await token_provider()
    except Exception:
        return None

    async def getter(path: str) -> dict:
        if get is not None:
            return await get(path)
        return await _default_get(cfg, token, path)

    try:
        data = await getter("/v1/billing/wallet")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return Wallet(
        balance=_int(data.get("balance")),
        cap=_int(data.get("cap")),
        plan=str(data.get("plan", "") or ""),
        status=str(data.get("status", "") or ""),
        included_tokens=_int(data.get("included_tokens")),
    )
```
- [ ] **Step 4: Run it, watch it pass.** `python -m pytest tests/test_wallet.py -q` → `5 passed`. Then `python -m py_compile src/webbee/wallet.py`.
- [ ] **Step 5: Commit.** `git add src/webbee/wallet.py tests/test_wallet.py && git commit -m "Add wallet client for Home credits tile"`.

---

### Task 2: `newtab_mode.py` — persisted new-tab default mode + `test_newtab_mode.py`

**Files**
- Create `src/webbee/newtab_mode.py`
- Create `tests/test_newtab_mode.py`
- Modify `tests/conftest.py` (add autouse cache-isolation fixture, after `_isolate_instance_lock_cache` ~line 30)

**Interfaces**
- Produces: `load_newtab_mode() -> str | None`; `save_newtab_mode(mode: str) -> None`; module `_CACHE_DIR` (test seam).

Grounding: mirrors `mode_store.py` fail-soft posture + the autopilot-never-persisted security rule, but a single process-wide marker `~/.cache/webbee/newtab-mode` (not per-repo).

- [ ] **Step 1: Write the failing test.** Create `tests/test_newtab_mode.py`:
```python
from webbee.newtab_mode import load_newtab_mode, save_newtab_mode


def _isolate(tmp_path, monkeypatch):
    import webbee.newtab_mode as NM
    monkeypatch.setattr(NM, "_CACHE_DIR", str(tmp_path / "webbee-cache"))


def test_none_when_no_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert load_newtab_mode() is None


def test_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    save_newtab_mode("plan")
    assert load_newtab_mode() == "plan"


def test_autopilot_downgraded_to_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    save_newtab_mode("autopilot")
    assert load_newtab_mode() == "default"


def test_save_never_raises_on_bad_dir(monkeypatch):
    import webbee.newtab_mode as NM
    monkeypatch.setattr(NM, "_CACHE_DIR", "/dev/null/nope")
    save_newtab_mode("plan")   # must not raise
    assert load_newtab_mode() is None
```
- [ ] **Step 2: Run it, watch it fail.** `python -m pytest tests/test_newtab_mode.py -q` → `ModuleNotFoundError`.
- [ ] **Step 3: Minimal impl.** Create `src/webbee/newtab_mode.py`:
```python
"""New-tab default mode (W5 Home Settings): the mode a NEW session tab opens
in, chosen from Home's Settings tile and remembered across restarts. Unlike
`mode_store` (per-repo, keyed by repo identity) this is ONE process-wide
preference -- a single marker file `~/.cache/webbee/newtab-mode`.

Same fail-soft posture as mode_store in BOTH directions (a missing/corrupt
file -> None; a write failure -> silently dropped) AND the same security
rule: autopilot is NEVER persisted -- `save_newtab_mode` downgrades an
autopilot write to 'default' before it touches disk, so a new tab never
silently resumes auto-approving every tool call from a stale file."""
from __future__ import annotations

import os

_CACHE_DIR = os.path.expanduser("~/.cache/webbee")   # test seam: monkeypatch this name
_MARKER = "newtab-mode"


def load_newtab_mode() -> "str | None":
    try:
        with open(os.path.join(_CACHE_DIR, _MARKER), "r", encoding="utf-8") as f:
            mode = f.read().strip()
        return mode or None
    except Exception:
        return None


def save_newtab_mode(mode: str) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        stored = mode if mode != "autopilot" else "default"
        with open(os.path.join(_CACHE_DIR, _MARKER), "w", encoding="utf-8") as f:
            f.write(stored)
    except Exception:
        pass
```
- [ ] **Step 4: Run it, watch it pass.** `python -m pytest tests/test_newtab_mode.py -q` → `4 passed`.
- [ ] **Step 5: Isolate the real cache suite-wide.** In `tests/conftest.py`, after the `_isolate_instance_lock_cache` fixture (~line 30) append:
```python
@pytest.fixture(autouse=True)
def _isolate_newtab_mode_cache(tmp_path, monkeypatch):
    """W5: Home's Settings tile persists the new-tab default mode to
    `~/.cache/webbee/newtab-mode` -- redirect it to a per-test tmp dir, same
    rationale as `_isolate_mode_cache` above (never touch the developer's
    real cache; keep every test hermetic)."""
    import webbee.newtab_mode as newtab_mode
    monkeypatch.setattr(newtab_mode, "_CACHE_DIR", str(tmp_path / "webbee-newtab-cache"))
```
- [ ] **Step 6: Full-suite sanity + compile.** `python -m pytest tests/test_newtab_mode.py tests/test_mode_store.py -q` → all pass; `python -m py_compile src/webbee/newtab_mode.py tests/conftest.py`.
- [ ] **Step 7: Commit.** `git add src/webbee/newtab_mode.py tests/test_newtab_mode.py tests/conftest.py && git commit -m "Add persisted new-tab default mode store"`.

---

### Task 3: `urlopen.py` — minimal best-effort URL opener + `test_urlopen.py`

**Files**
- Create `src/webbee/urlopen.py`
- Create `tests/test_urlopen.py`

**Interfaces**
- Produces: `open_url(url: str) -> str` (best-effort `webbrowser.open`; returns the URL unchanged; never raises).

Grounding: no opener exists in the client (verified: only `config.py` references `panel_url`; `account.login_device_flow` merely *prints* URLs). Over SSH `webbrowser` no-ops/fails, so callers ALSO surface the URL as copyable text (Home shows `data.notice`).

- [ ] **Step 1: Write the failing test.** Create `tests/test_urlopen.py`:
```python
import webbee.urlopen as urlopen


def test_open_url_returns_url_and_calls_webbrowser(monkeypatch):
    calls = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u, new=2: calls.append(u))
    out = urlopen.open_url("https://panel.imperal.io/billing")
    assert out == "https://panel.imperal.io/billing"
    assert calls == ["https://panel.imperal.io/billing"]


def test_open_url_never_raises(monkeypatch):
    import webbrowser
    def boom(*a, **k):
        raise RuntimeError("no display")
    monkeypatch.setattr(webbrowser, "open", boom)
    assert urlopen.open_url("https://x") == "https://x"
```
- [ ] **Step 2: Run it, watch it fail.** `python -m pytest tests/test_urlopen.py -q` → `ModuleNotFoundError`.
- [ ] **Step 3: Minimal impl.** Create `src/webbee/urlopen.py`:
```python
"""Best-effort URL opener (W5 Home: Top-up credits, Read security docs).
There is no existing opener in the client -- this is the minimal one. On a
LOCAL machine `webbrowser.open` launches the default browser; over SSH (the
common Webbee Code case) it no-ops or fails, so the caller ALSO surfaces the
URL as copyable text. Returns the URL unchanged so the caller can show it;
never raises."""
from __future__ import annotations


def open_url(url: str) -> str:
    try:
        import webbrowser
        webbrowser.open(url, new=2)
    except Exception:
        pass
    return url
```
- [ ] **Step 4: Run it, watch it pass.** `python -m pytest tests/test_urlopen.py -q` → `2 passed`; `python -m py_compile src/webbee/urlopen.py`.
- [ ] **Step 5: Commit.** `git add src/webbee/urlopen.py tests/test_urlopen.py && git commit -m "Add minimal best-effort URL opener"`.

---

### Task 4: `WorkspaceResources.roots()` — recent-repos source accessor

**Files**
- Modify `src/webbee/slots.py` (add method after `bundles()`, ~line 252)
- Modify `tests/test_slots.py` (add one test)

**Interfaces**
- Produces: `WorkspaceResources.roots() -> list[str]` — the realpath key of every booted repo root, insertion order.

Grounding: `bundles()` (slots.py:246-252) returns the per-root VALUE dicts (`{"intel","watcher_task","shadow","git_branch"}` — verified `boot.py:136`), which carry **no path**. Home's recent-repos tile needs the KEYS. This adds the missing public accessor (resolving the spec's "distinct booted repo roots via `resources.bundles()`" anchor, which as written returns values, not paths).

- [ ] **Step 1: Write the failing test.** Append to `tests/test_slots.py`:
```python
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
```
- [ ] **Step 2: Run it, watch it fail.** `python -m pytest tests/test_slots.py::test_workspace_resources_roots_lists_booted_paths -q` → `AttributeError: 'WorkspaceResources' object has no attribute 'roots'`.
- [ ] **Step 3: Minimal impl.** In `src/webbee/slots.py`, immediately after the `bundles()` method (~line 252) add:
```python
    def roots(self) -> list[str]:
        """PUBLIC accessor (W5 Home recent-repos tile) — the realpath of every
        distinct repo root this process has booted a workspace for, insertion
        order. `bundles()` returns the per-root VALUE bundles; `roots()`
        returns their KEYS (the paths), which Home turns into one-click
        "open a new tab here" actions."""
        return list(self._by_root.keys())
```
- [ ] **Step 4: Run it, watch it pass.** `python -m pytest tests/test_slots.py -q` → all pass; `python -m py_compile src/webbee/slots.py`.
- [ ] **Step 5: Commit.** `git add src/webbee/slots.py tests/test_slots.py && git commit -m "Add WorkspaceResources.roots() accessor for recent repos"`.

---

### Task 5: `home_view.py` pure core — snapshots + actionable-item model + builders + `test_home_view.py` (part 1)

**Files**
- Create `src/webbee/home_view.py` (pure section only this task; `HomeView` class added in Task 6)
- Create `tests/test_home_view.py`

**Interfaces**
- Consumes: `webbee.wallet.Wallet`, `webbee.account.Account`, a live `SlotManager` (for `tab_rows`).
- Produces: dataclasses `DeviceRow`, `TabRow`, `HomeData`, `ActionItem`, `HomeActions`; class `HomeModel`; pure fns `tab_rows(slots) -> list[TabRow]`, `build_home_model(data, tabs, actions) -> HomeModel`, `two_column(width, threshold=100) -> bool`, `_cycle(options, current, delta) -> str`; constants `NOTIFY_OPTIONS`, `MODE_OPTIONS`, `SECURITY_LINES`, `SECURITY_DOCS_URL`.

**No prompt_toolkit import in this section** (tabs.py discipline).

- [ ] **Step 1: Write the failing test.** Create `tests/test_home_view.py`:
```python
from types import SimpleNamespace

from webbee.account import Account
from webbee.wallet import Wallet
from webbee.home_view import (MODE_OPTIONS, NOTIFY_OPTIONS, ActionItem,
                              HomeActions, HomeData, HomeModel, TabRow,
                              build_home_model, tab_rows, two_column, _cycle)
from webbee.slots import SessionSlot, SlotManager


def _rec():
    """A HomeActions whose every callback records its call into `log`."""
    log = []
    actions = HomeActions(
        new_session=lambda: log.append(("new_session",)),
        open_recent=lambda p: log.append(("open_recent", p)),
        switch_tab=lambda i: log.append(("switch_tab", i)),
        close_tab=lambda i: log.append(("close_tab", i)),
        set_tab_mode=lambda i, m: log.append(("set_tab_mode", i, m)),
        set_notify=lambda a: log.append(("set_notify", a)),
        set_new_tab_mode=lambda m: log.append(("set_new_tab_mode", m)),
        top_up=lambda: log.append(("top_up",)),
        open_security_docs=lambda: log.append(("open_security_docs",)),
    )
    return actions, log


class _StatusSink:
    def __init__(self, tokens, credits):
        self._t, self._c = tokens, credits
    def status(self):
        return {"tokens": self._t, "credits": self._c}
    def consent_pending(self):
        return False
    def is_busy(self):
        return False


def _slots_with_one_session():
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace="/w", label="Home",
                        pane=object(), sink=None, agent=None))
    s = SessionSlot(kind="session", workspace="/w", label="myrepo",
                    pane=object(), sink=_StatusSink(2100, 7), agent=None)
    s.mode = "plan"
    mgr.add(s)
    mgr.active_idx = 1
    return mgr


def test_cycle_wraps_both_directions():
    assert _cycle(MODE_OPTIONS, "default", +1) == "plan"
    assert _cycle(MODE_OPTIONS, "autopilot", +1) == "default"
    assert _cycle(MODE_OPTIONS, "default", -1) == "autopilot"
    assert _cycle(NOTIFY_OPTIONS, "off", +1) == "panel"


def test_two_column_threshold():
    assert two_column(120) is True
    assert two_column(80) is False


def test_tab_rows_reads_spend_glyph_and_active():
    rows = tab_rows(_slots_with_one_session())
    assert rows == [TabRow(idx=1, label="myrepo", mode="plan", glyph="○",
                           tokens=2100, credits=7, active=True)]


def test_tab_rows_survives_status_raising():
    class _Boom:
        def status(self):
            raise RuntimeError("x")
        def consent_pending(self):
            return False
        def is_busy(self):
            return False
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace="/w", label="Home",
                        pane=object(), sink=None, agent=None))
    mgr.add(SessionSlot(kind="session", workspace="/w", label="r",
                        pane=object(), sink=_Boom(), agent=None))
    rows = tab_rows(mgr)
    assert rows[0].tokens == 0 and rows[0].credits == 0


def test_build_model_item_ids_and_order():
    actions, _ = _rec()
    data = HomeData(account=Account(signed_in=True, nickname="v", plan="pro"),
                    wallet=Wallet(balance=100, cap=500), recent=["/one", "/two"],
                    notify_state="panel", new_tab_mode="plan")
    tabs = tab_rows(_slots_with_one_session())
    m = build_home_model(data, tabs, actions)
    ids = [it.id for it in m.items]
    assert ids == [
        "new-session",
        "tab-1", "tab-mode-1", "tab-close-1",
        "recent:/one", "recent:/two",
        "set-newtab-mode", "set-notify", "top-up", "security-docs",
    ]


def test_new_session_and_recent_dispatch():
    actions, log = _rec()
    data = HomeData(recent=["/one"])
    m = build_home_model(data, [], actions)
    m.focus_id("new-session"); m.activate()
    m.focus_id("recent:/one"); m.activate()
    assert log == [("new_session",), ("open_recent", "/one")]


def test_segmented_left_right_cycle_new_tab_mode():
    actions, log = _rec()
    data = HomeData(new_tab_mode="default")
    m = build_home_model(data, [], actions)
    m.focus_id("set-newtab-mode")
    m.right(); m.left()
    assert log == [("set_new_tab_mode", "plan"), ("set_new_tab_mode", "autopilot")]


def test_per_tab_mode_and_close_dispatch():
    actions, log = _rec()
    tabs = tab_rows(_slots_with_one_session())
    m = build_home_model(HomeData(), tabs, actions)
    m.focus_id("tab-mode-1"); m.right()          # plan -> autopilot
    m.focus_id("tab-close-1"); m.activate()
    m.focus_id("tab-1"); m.activate()
    assert log == [("set_tab_mode", 1, "autopilot"), ("close_tab", 1), ("switch_tab", 1)]


def test_notify_disabled_and_skipped_by_nav_when_no_session():
    actions, log = _rec()
    m = build_home_model(HomeData(notify_state="off"), [], actions)   # no tabs -> no session
    notify = [it for it in m.items if it.id == "set-notify"][0]
    assert notify.enabled is False
    # nav never lands on a disabled item
    m.focus_id("set-newtab-mode")
    m.focus_next()   # would be set-notify, but it's disabled -> skip to top-up
    assert m.focused().id == "top-up"
    m.right()        # activating a skipped disabled control never dispatches
    assert ("set_notify", "panel") not in log


def test_notify_enabled_when_a_session_exists():
    actions, _ = _rec()
    tabs = tab_rows(_slots_with_one_session())
    m = build_home_model(HomeData(notify_state="tg"), tabs, actions)
    notify = [it for it in m.items if it.id == "set-notify"][0]
    assert notify.enabled is True and notify.value == "tg"


def test_focus_nav_wraps_over_enabled_items():
    actions, _ = _rec()
    m = build_home_model(HomeData(), [], actions)   # items: new-session, set-newtab-mode, set-notify(disabled), top-up, security-docs
    assert m.focused().id == "new-session"
    m.focus_prev()                                  # wrap backward to last enabled
    assert m.focused().id == "security-docs"
    m.focus_next()                                  # wrap forward to first enabled
    assert m.focused().id == "new-session"
```
- [ ] **Step 2: Run it, watch it fail.** `python -m pytest tests/test_home_view.py -q` → `ModuleNotFoundError: No module named 'webbee.home_view'`.
- [ ] **Step 3: Minimal impl.** Create `src/webbee/home_view.py` with the pure core (NO prompt_toolkit import):
```python
"""Home dashboard (W5) — the interactive "website inside the terminal" that
replaces Home's flat skeleton. This module owns BOTH the pure, unit-testable
core (this section: snapshot dataclasses + the actionable-item model +
focus/nav/dispatch + pure builders, with NO prompt_toolkit import) AND the
`HomeView` render component below it (fragments + a focusable Window,
mirroring OutputPane's `.window`). `home.py` keeps the async fetch
orchestration and feeds a `HomeData` here; every interaction repaints via
`get_app().invalidate()`, never a `fill_home` re-run."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from webbee.account import Account
from webbee.wallet import Wallet

NOTIFY_OPTIONS = ("off", "panel", "tg", "both")
MODE_OPTIONS = ("default", "plan", "autopilot")

# Trust/Security tile — public-facing product copy, subject to the ICNLI
# claims doctrine (Locus of Authority). Every line is a real, verifiable
# system property. CONFIRM against ~/Nextcloud/MCP-Marketing-Imperal/
# 07-voice-and-messaging.md + the exact docs URL on docs.imperal.io before
# ship (Task 11). Default below is concrete and defensible.
SECURITY_DOCS_URL = "https://docs.imperal.io/en/security/overview/"
SECURITY_LINES = (
    "Your private data is masked before the model sees it.",
    "Every risky or paid action asks you first.",
    "Encrypted in transit to auth.imperal.io.",
)


@dataclass
class DeviceRow:
    label: str          # human surface name — never a raw id/IP (PII)
    current: bool = False


@dataclass
class TabRow:
    idx: int
    label: str
    mode: str
    glyph: str
    tokens: int
    credits: int
    active: bool


@dataclass
class HomeData:
    account: Account | None = None
    wallet: Wallet | None = None
    devices: list = field(default_factory=list)     # list[DeviceRow]
    recent: list = field(default_factory=list)      # list[str] repo root paths
    notify_state: str = ""       # "off"|"panel"|"tg"|"both"|"" (unknown)
    remote_desc: str = ""        # human line (display)
    new_tab_mode: str = "default"
    intel_enabled: bool = True
    intel_status: str = ""       # e.g. "42 files indexed" / ""
    endpoint: str = ""
    version: str = ""
    update_notice: str = ""
    notice: str = ""             # transient one-line note (top-up/security URL)


@dataclass
class ActionItem:
    id: str
    label: str
    hint: str
    activate: "Callable[[], None] | None" = None
    left: "Callable[[], None] | None" = None
    right: "Callable[[], None] | None" = None
    enabled: bool = True
    value: str = ""              # current segmented value (display only)


@dataclass
class HomeActions:
    new_session: "Callable[[], None]"
    open_recent: "Callable[[str], None]"
    switch_tab: "Callable[[int], None]"
    close_tab: "Callable[[int], None]"
    set_tab_mode: "Callable[[int, str], None]"
    set_notify: "Callable[[str], None]"
    set_new_tab_mode: "Callable[[str], None]"
    top_up: "Callable[[], None]"
    open_security_docs: "Callable[[], None]"


class HomeModel:
    """The ordered list of focusable items + focus/hover state + nav/dispatch.
    PURE. `focus_idx` indexes items in visual order; disabled items are
    rendered (greyed) but SKIPPED by nav and never dispatch."""

    def __init__(self, items: "list[ActionItem]"):
        self.items = items
        self.focus_idx = 0
        self.hover_id: "str | None" = None
        for i, it in enumerate(items):
            if it.enabled:
                self.focus_idx = i
                break

    def _enabled(self) -> "list[int]":
        return [i for i, it in enumerate(self.items) if it.enabled]

    def focused(self) -> "ActionItem | None":
        if 0 <= self.focus_idx < len(self.items):
            return self.items[self.focus_idx]
        return None

    def move(self, delta: int) -> None:
        order = self._enabled()
        if not order:
            return
        cur = self.focus_idx if self.focus_idx in order else order[0]
        self.focus_idx = order[(order.index(cur) + delta) % len(order)]

    def focus_next(self) -> None:
        self.move(1)

    def focus_prev(self) -> None:
        self.move(-1)

    def activate(self) -> None:
        it = self.focused()
        if it is not None and it.enabled and it.activate is not None:
            it.activate()

    def left(self) -> None:
        it = self.focused()
        if it is not None and it.enabled and it.left is not None:
            it.left()

    def right(self) -> None:
        it = self.focused()
        if it is not None and it.enabled and it.right is not None:
            it.right()

    def focus_id(self, item_id: "str | None") -> None:
        if item_id is None:
            return
        for i, it in enumerate(self.items):
            if it.id == item_id and it.enabled:
                self.focus_idx = i
                return


def _cycle(options, current: str, delta: int) -> str:
    try:
        i = options.index(current)
    except ValueError:
        i = 0
    return options[(i + delta) % len(options)]


def two_column(width: int, threshold: int = 100) -> bool:
    """Wide terminal -> You + Wallet render side-by-side; narrow -> stacked."""
    return width >= threshold


def tab_rows(slots) -> "list[TabRow]":
    """PURE transform of the live SlotManager into Home's per-tab rows
    (session tabs only -- Home never lists itself). Per-tab spend comes from
    the slot's own `sink.status()` ({tokens,credits}); a None sink or a
    status() that raises degrades to 0/0, never an exception."""
    rows: "list[TabRow]" = []
    active_idx = slots.active_idx
    for i, s in enumerate(slots.slots):
        if s.kind != "session":
            continue
        tokens = credits = 0
        sink = s.sink
        if sink is not None:
            try:
                st = sink.status()
                tokens = int(st.get("tokens", 0) or 0)
                credits = int(st.get("credits", 0) or 0)
            except Exception:
                pass
        rows.append(TabRow(idx=i, label=s.label or "", mode=s.mode,
                           glyph=s.status_glyph(), tokens=tokens,
                           credits=credits, active=(i == active_idx)))
    return rows


def build_home_model(data: "HomeData", tabs: "list[TabRow]",
                     actions: "HomeActions") -> "HomeModel":
    """Build the ordered actionable-item list. Notifications are DISABLED
    (greyed, nav-skipped) unless a session tab exists -- remote routing is
    per live session (spec §5). New-tab mode / top-up / security are always
    enabled (they need no live session)."""
    items: "list[ActionItem]" = []

    items.append(ActionItem(
        id="new-session", label="+ New session",
        hint="open a new session tab (Ctrl+T)",
        activate=actions.new_session))

    for t in tabs:
        items.append(ActionItem(
            id=f"tab-{t.idx}", label=t.label or f"tab {t.idx}",
            hint="switch to this tab",
            activate=(lambda idx=t.idx: actions.switch_tab(idx))))
        items.append(ActionItem(
            id=f"tab-mode-{t.idx}", label=t.mode, value=t.mode,
            hint="←→ change this tab's mode",
            left=(lambda idx=t.idx, cur=t.mode: actions.set_tab_mode(idx, _cycle(MODE_OPTIONS, cur, -1))),
            right=(lambda idx=t.idx, cur=t.mode: actions.set_tab_mode(idx, _cycle(MODE_OPTIONS, cur, +1)))))
        items.append(ActionItem(
            id=f"tab-close-{t.idx}", label="✕",
            hint="close this tab (the run keeps going server-side)",
            activate=(lambda idx=t.idx: actions.close_tab(idx))))

    for path in data.recent:
        name = path.rstrip("/").rsplit("/", 1)[-1] or path
        items.append(ActionItem(
            id=f"recent:{path}", label=name,
            hint=f"open a new tab in {path}",
            activate=(lambda p=path: actions.open_recent(p))))

    items.append(ActionItem(
        id="set-newtab-mode", label=data.new_tab_mode, value=data.new_tab_mode,
        hint="←→ change the mode new tabs open in",
        left=(lambda: actions.set_new_tab_mode(_cycle(MODE_OPTIONS, data.new_tab_mode, -1))),
        right=(lambda: actions.set_new_tab_mode(_cycle(MODE_OPTIONS, data.new_tab_mode, +1)))))

    has_session = bool(tabs)
    notify_val = data.notify_state or "off"
    items.append(ActionItem(
        id="set-notify", label=notify_val, value=notify_val,
        hint=("←→ where this session mirrors/steers"
              if has_session else "start a session first to route notifications"),
        left=(lambda: actions.set_notify(_cycle(NOTIFY_OPTIONS, notify_val, -1))),
        right=(lambda: actions.set_notify(_cycle(NOTIFY_OPTIONS, notify_val, +1))),
        enabled=has_session))

    items.append(ActionItem(
        id="top-up", label="Top up credits",
        hint="open the billing page to add credits",
        activate=actions.top_up))

    items.append(ActionItem(
        id="security-docs", label="Read our security & privacy →",
        hint="open the security & privacy documentation",
        activate=actions.open_security_docs))

    return HomeModel(items)
```
- [ ] **Step 4: Run it, watch it pass.** `python -m pytest tests/test_home_view.py -q` → all pass; `python -m py_compile src/webbee/home_view.py`.
- [ ] **Step 5: Commit.** `git add src/webbee/home_view.py tests/test_home_view.py && git commit -m "Add Home dashboard pure model (data, items, focus/nav, builders)"`.

---

### Task 6: `HomeView` render component — fragments, window, duck-typed pane surface + `test_home_view.py` (part 2)

**Files**
- Modify `src/webbee/home_view.py` (append the `HomeView` class + fragment/layout helpers)
- Modify `tests/test_home_view.py` (append fragment-structure tests)

**Interfaces**
- Consumes: `HomeData`, `HomeActions`, a live `SlotManager`; prompt_toolkit `Window`/`FormattedTextControl`/`MouseEventType` (imported INSIDE methods only).
- Produces: `class HomeView` exposing `.window`, `.console`, `.data`; interaction methods `move_focus(delta)`, `focus_next()`, `focus_prev()`, `activate_focused()`, `seg_left()`, `seg_right()`, `notify()`; OutputPane-compat surface `.reflow(w)`, `.edge_tick()`, `.flash()`, `.scroll(delta)`, `._view_h`, `.forward_mouse(ev, clamp=...)`; plus module helpers `_line_len`, `_pad_line`, `_side_by_side`.

The `HomeView.window` is `Window(FormattedTextControl(self._fragments, focusable=True, show_cursor=False), wrap_lines=False, always_hide_cursor=True)`, mirroring `OutputPane.window` (output_pane.py:78) so `DynamicContainer(lambda: _pane().window)` (tui.py:1142) is unchanged. Fragment/handler discipline mirrors `tabs.py:68-83,111-118`. Focus/hover persist across frame rebuilds by **id** (`self._focus_id`/`self._hover_id`), since indices shift when tabs open/close.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_home_view.py`:
```python
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from webbee.home_view import HomeView, _side_by_side, _line_len, _pad_line


def _up(handler):
    ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    return handler(ev)


def _view(data=None, slots=None, width=120):
    actions, log = _rec()
    if slots is None:
        slots = _slots_with_one_session()
    hv = HomeView(slots=slots, actions=actions, data=data or HomeData(), width=width)
    return hv, log


def _frag_with_text(frags, text):
    for f in frags:
        if f[1] == text:
            return f
    raise AssertionError(f"{text!r} not in fragments")


def test_side_by_side_pads_left_column():
    left = [[("s", "ab")], [("s", "c")]]
    right = [[("s", "XY")]]
    rows = _side_by_side(left, right, colw=5, gap=2)
    assert _line_len(rows[0]) == 5 + 2 + 2      # padded left + gap + right
    assert _line_len(rows[1]) == 5 + 2 + 0      # right shorter -> empty


def test_every_action_item_label_carries_a_handler():
    data = HomeData(account=Account(signed_in=True, nickname="v", plan="pro"),
                    wallet=Wallet(balance=100, cap=500), recent=["/one"])
    hv, _ = _view(data=data)
    frags = hv._fragments()
    for label in ("+ New session", "myrepo", "[plan]", "✕", "one",
                  "Top up credits", "Read our security & privacy →"):
        f = _frag_with_text(frags, label)
        assert len(f) == 3 and callable(f[2])   # 3-tuple with a mouse handler


def test_focused_item_carries_focus_style():
    hv, _ = _view()
    hv._focus_id = "top-up"
    f = _frag_with_text(hv._fragments(), "Top up credits")
    assert f[0] == "class:home.focus"


def test_hovered_item_carries_focus_style():
    hv, _ = _view()
    hv._hover_id = "security-docs"
    f = _frag_with_text(hv._fragments(), "Read our security & privacy →")
    assert f[0] == "class:home.focus"


def test_click_activates_and_moves_focus():
    hv, log = _view(data=HomeData(recent=["/one"]))
    f = _frag_with_text(hv._fragments(), "+ New session")
    _up(f[2])
    assert ("new_session",) in log
    assert hv._focus_id == "new-session"


def test_mouse_move_sets_hover():
    hv, _ = _view()
    f = _frag_with_text(hv._fragments(), "Top up credits")
    ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_MOVE,
                    button=MouseButton.NONE, modifiers=frozenset())
    f[2](ev)
    assert hv._hover_id == "top-up"


def test_scroll_event_falls_through():
    hv, _ = _view()
    f = _frag_with_text(hv._fragments(), "Top up credits")
    ev = MouseEvent(position=Point(0, 0), event_type=MouseEventType.SCROLL_UP,
                    button=MouseButton.NONE, modifiers=frozenset())
    assert f[2](ev) is NotImplemented


def test_narrow_width_stacks_you_and_wallet():
    data = HomeData(account=Account(signed_in=True, nickname="v"),
                    wallet=Wallet(balance=5))
    hv, _ = _view(data=data, width=70)
    text = "".join(f[1] for f in hv._fragments())
    # both tile headers present, and (narrow) on different lines
    lines = text.split("\n")
    you = [i for i, ln in enumerate(lines) if "You" in ln]
    wal = [i for i, ln in enumerate(lines) if "Wallet" in ln]
    assert you and wal and you[0] != wal[0]


def test_public_nav_methods_persist_focus_by_id():
    hv, _ = _view(data=HomeData(recent=["/one"]))
    hv.focus_next()                     # new-session -> tab-1
    assert hv._focus_id == "tab-1"
    hv.focus_prev()
    assert hv._focus_id == "new-session"


def test_outputpane_compat_surface_is_safe():
    hv, _ = _view()
    assert hv.flash() == ""
    hv.edge_tick()                      # no-op, never raises
    hv.scroll(-5)                       # no-op
    assert isinstance(hv._view_h, int)
    assert hv.forward_mouse(object()) is False
    hv.reflow(90)
    assert hv.console.width == 90
```
- [ ] **Step 2: Run it, watch it fail.** `python -m pytest tests/test_home_view.py -q` → `ImportError: cannot import name 'HomeView'`.
- [ ] **Step 3: Minimal impl.** Append to `src/webbee/home_view.py` (module helpers first, then the class):
```python
# ---- fragment layout helpers (PURE) ----------------------------------------

def _line_len(frags) -> int:
    return sum(len(f[1]) for f in frags)


def _pad_line(frags, width: int):
    n = _line_len(frags)
    return list(frags) + ([("", " " * (width - n))] if n < width else [])


def _side_by_side(left, right, colw: int, gap: int = 3):
    """Zip two column line-lists into side-by-side fragment rows. Left column
    padded to `colw`; missing rows on either side render blank. ASCII-width
    only (Home avoids double-width glyphs so len()==cells)."""
    rows = []
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else []
        r = right[i] if i < len(right) else []
        rows.append(_pad_line(l, colw) + [("", " " * gap)] + r)
    return rows


def _fit(label: str, max_len: int) -> str:
    label = label or ""
    if len(label) <= max_len:
        return label
    if max_len <= 1:
        return label[:1]
    return label[:max_len - 1] + "…"


class HomeView:
    """Interactive Home. Owns render + interaction; the Home slot's `.pane`.
    Duck-types the OutputPane surface the dock's ticker/layout touch so
    `DynamicContainer(lambda: _pane().window)` (tui.py:1142) and `_tick_once`
    (tui.py:175-197) keep working with Home's pane in place unchanged."""

    def __init__(self, *, slots, actions: "HomeActions",
                 data: "HomeData | None" = None, width: int = 100):
        from rich.console import Console
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        self.slots = slots
        self.actions = actions
        self.data = data if data is not None else HomeData()
        self.console = Console(width=width)      # width tracking for _width_watch + columns
        self._focus_id: "str | None" = None
        self._hover_id: "str | None" = None
        self._model: "HomeModel | None" = None
        self.control = FormattedTextControl(self._fragments, focusable=True, show_cursor=False)
        self.window = Window(content=self.control, wrap_lines=False, always_hide_cursor=True)

    # ---- OutputPane-compatible surface (ticker/layout duck-type) ----------
    def reflow(self, new_width: int) -> None:
        if new_width and new_width != self.console.width:
            self.console.width = new_width
            self._invalidate()

    def edge_tick(self) -> None:
        return None

    def flash(self) -> str:
        return ""

    def scroll(self, delta: int) -> None:
        return None

    @property
    def _view_h(self) -> int:
        return 20

    def forward_mouse(self, ev, clamp: str = "bottom") -> bool:
        return False

    def notify(self) -> None:
        self._invalidate()

    def _invalidate(self) -> None:
        try:
            from prompt_toolkit.application import get_app_or_none
            app = get_app_or_none()
            if app is not None:
                app.invalidate()
        except Exception:
            pass

    # ---- model lifecycle --------------------------------------------------
    def _build_model(self) -> "HomeModel":
        tabs = tab_rows(self.slots)
        m = build_home_model(self.data, tabs, self.actions)
        m.focus_id(self._focus_id)                           # no-op if gone/disabled
        self._focus_id = m.focused().id if m.focused() is not None else None
        m.hover_id = self._hover_id
        self._model = m
        return m

    def move_focus(self, delta: int) -> None:
        m = self._build_model(); m.move(delta)
        self._focus_id = m.focused().id if m.focused() is not None else None
        self._invalidate()

    def focus_next(self) -> None:
        self.move_focus(1)

    def focus_prev(self) -> None:
        self.move_focus(-1)

    def activate_focused(self) -> None:
        self._build_model().activate()
        self._invalidate()

    def seg_left(self) -> None:
        self._build_model().left()
        self._invalidate()

    def seg_right(self) -> None:
        self._build_model().right()
        self._invalidate()

    # ---- mouse ------------------------------------------------------------
    def _item_handler(self, item: "ActionItem"):
        def _h(mouse_event):
            from prompt_toolkit.mouse_events import MouseEventType
            et = mouse_event.event_type
            if et == MouseEventType.MOUSE_MOVE:
                if self._hover_id != item.id:
                    self._hover_id = item.id
                    self._invalidate()
                return None
            if et == MouseEventType.MOUSE_UP:
                if not item.enabled:
                    return None
                self._focus_id = item.id
                if item.activate is not None:
                    item.activate()
                elif item.right is not None:
                    item.right()
                self._invalidate()
                return None
            return NotImplemented
        return _h

    # ---- render -----------------------------------------------------------
    def _fragments(self):
        from webbee.home import _mask_email
        from webbee.render import _fmt_tokens
        m = self._build_model()
        by_id = {it.id: it for it in m.items}
        focused = m.focused()
        focus_id = focused.id if focused is not None else None
        hover_id = m.hover_id
        width = max(20, self.console.width or 80)

        def sfor(item):
            if item.id == focus_id or item.id == hover_id:
                return "class:home.focus"
            if not item.enabled:
                return "class:home.disabled"
            return "class:home.item"

        def act(item, text):
            return (sfor(item), text, self._item_handler(item))

        def hdr(title):
            return [("class:home.header", f" {title} ")]

        def you_lines():
            a = self.data.account
            L = [hdr("You")]
            if a is None or not getattr(a, "signed_in", False):
                return L + [[("class:home.dim", "  …")]]
            nick = getattr(a, "nickname", "") or ""
            L.append([("class:home.value", f"  @{nick}" if nick else "  (no nickname)")])
            plan = getattr(a, "plan", "") or ""
            if plan:
                status = getattr(a, "plan_status", "") or ""
                tag = f" ({status})" if status and status != "active" else ""
                L.append([("class:home.dim", f"  {plan} plan{tag}")])
            email = getattr(a, "email", "") or ""
            if email:
                L.append([("class:home.dim", f"  {_mask_email(email)}")])
            return L

        def wallet_lines():
            w = self.data.wallet
            L = [hdr("Wallet")]
            if w is None:
                L.append([("class:home.dim", "  credits —")])
            else:
                cap = f" / {_fmt_tokens(w.cap)}" if w.cap else ""
                L.append([("class:home.value", f"  {_fmt_tokens(w.balance)}{cap} credits")])
            it = by_id["top-up"]
            L.append([("class:home.dim", "  "), act(it, "Top up credits")])
            return L

        def start_lines():
            it = by_id["new-session"]
            return [hdr("Start"),
                    [("class:home.dim", "  "), act(it, "+ New session")],
                    [("class:home.dim", "  or type a task here to open a tab and start")]]

        def tabs_lines():
            L = [hdr("Your tabs")]
            tabs = tab_rows(self.slots)
            if not tabs:
                return L + [[("class:home.dim", "  no tabs open yet — type a task to start")]]
            for t in tabs:
                sw, md, cl = by_id[f"tab-{t.idx}"], by_id[f"tab-mode-{t.idx}"], by_id[f"tab-close-{t.idx}"]
                marker = "●" if t.active else "○"
                L.append([
                    ("class:home.dim", f"  {marker} {t.idx} "),
                    act(sw, _fit(t.label or f"tab {t.idx}", 24)),
                    ("class:home.dim", f"  {t.glyph}  "),
                    act(md, f"[{md.value}]"),
                    ("class:home.dim", f"  {_fmt_tokens(t.tokens)} tok · {_fmt_tokens(t.credits)} cr  "),
                    act(cl, "✕"),
                ])
            return L

        def recent_lines():
            L = [hdr("Recent repos")]
            if not self.data.recent:
                return L + [[("class:home.dim", "  —")]]
            for path in self.data.recent:
                it = by_id[f"recent:{path}"]
                name = _fit(path.rstrip("/").rsplit("/", 1)[-1] or path, 28)
                L.append([("class:home.dim", "  ▸ "), act(it, name)])
            return L

        def devices_lines():
            L = [hdr("Your devices")]
            if not self.data.devices:
                return L + [[("class:home.dim", "  …")]]
            for d in self.data.devices:
                tag = " (this one)" if d.current else ""
                L.append([("class:home.dim", f"  • {_fit(d.label, 32)}{tag}")])
            return L

        def settings_lines():
            L = [hdr("Settings")]
            ntm, nt = by_id["set-newtab-mode"], by_id["set-notify"]
            L.append([("class:home.dim", "  new tabs open in  "), act(ntm, f"[{ntm.value}]")])
            L.append([("class:home.dim", "  notifications     "),
                      (sfor(nt), f"[{nt.value}]", self._item_handler(nt))])
            d = self.data
            intel = d.intel_status or ("on" if d.intel_enabled else "off")
            L.append([("class:home.dim", f"  code intel {intel} · {d.endpoint} · v{d.version}")])
            if d.update_notice:
                L.append([("class:home.dim", f"  {d.update_notice}")])
            return L

        def security_lines():
            it = by_id["security-docs"]
            L = [hdr("Trust & security")]
            for line in SECURITY_LINES:
                L.append([("class:home.dim", f"  {line}")])
            L.append([("class:home.dim", "  "), act(it, "Read our security & privacy →")])
            return L

        def footer_lines():
            hint = ""
            if hover_id and hover_id in by_id:
                hint = by_id[hover_id].hint
            elif focused is not None:
                hint = focused.hint
            L = [[("", "")], [("class:home.hint", f"  {hint}")]]
            if self.data.notice:
                L.append([("class:home.dim", f"  {self.data.notice}")])
            L.append([("class:home.dim",
                       "  ↑↓ move · ↵ open · ←→ change "
                       "· Ctrl+T new tab · Alt+N switch")])
            return L

        lines = [[("class:home.header", " ◆ Home ")], [("", "")]]
        if two_column(width):
            colw = max(20, (width // 2) - 3)
            lines += _side_by_side(you_lines(), wallet_lines(), colw)
        else:
            lines += you_lines() + [[("", "")]] + wallet_lines()
        lines += [[("", "")]] + start_lines()
        lines += [[("", "")]] + tabs_lines()
        lines += [[("", "")]] + recent_lines()
        lines += [[("", "")]] + devices_lines()
        lines += [[("", "")]] + settings_lines()
        lines += [[("", "")]] + security_lines()
        lines += footer_lines()

        out = []
        for ln in lines:
            out.extend(ln)
            out.append(("", "\n"))
        return out
```
- [ ] **Step 4: Run it, watch it pass.** `python -m pytest tests/test_home_view.py -q` → all pass; `python -m py_compile src/webbee/home_view.py`.
- [ ] **Step 5: LIVE-ONLY note.** Hover cursor-tracking (`?1003` MOUSE_MOVE) and cursor-off-item hover clearing are NOT headless-testable — verified live in Task 10 on Valentin's terminal. This task tests only that MOUSE_MOVE sets `hover_id` and the hovered fragment carries the highlight class.
- [ ] **Step 6: Commit.** `git add src/webbee/home_view.py tests/test_home_view.py && git commit -m "Add HomeView render component (fragments, window, mouse, focus/hover)"`.

---

### Task 7: Rewire `home.py` `fill_home` to populate `HomeData`; delete per-tile renderers

**Files**
- Modify `src/webbee/home.py` (delete `_render_header`, `_render_hint`, `render_skeleton`, `render_identity`, `render_slots_tile`, `render_repo_tile`, `render_system_tile`, `HOME_HINT`, `_SECTIONS`; keep `_mask_email`, `is_stale`, `_pick_session_slot`; add `_notify_state_from`; rewrite `fill_home` signature + body)
- Modify `tests/test_home.py` (drop render_* imports/tests; rewrite `fill_home` tests against `HomeData`; keep `_mask_email`/`is_stale`/`_pick_session_slot`/`_home_target_workspace`/`_home_input`/`_schedule_home_refill` tests)

**Interfaces**
- Produces: `async def fill_home(slot, *, cfg, token_provider, slots, account_fetcher, sessions_client, resources, version, wallet_fetcher=None) -> None`; `_notify_state_from(state: dict) -> str`.
- Consumes: `slot.pane` is a `HomeView` with `.data: HomeData` and `.notify()`; `resources.roots()` (Task 4); `wallet.fetch_wallet`; `sessions_client.list_sessions`; `intel.repo_profile()`; `remote.get_remote`/`describe`; `update.check_for_update`.

Grounding: reuses the existing best-effort orchestration shape (home.py:206-290) — `_filling` re-entrancy guard, `_last_fill` stamp, staged repaints — but writes fields onto `HomeData` and repaints via `view.notify()` instead of `console.clear()`+render. Verified: no module outside `home.py`/`test_home.py` imports the render_* functions.

- [ ] **Step 1: Write the failing tests.** Replace the `fill_home` section of `tests/test_home.py` and its top imports. New top imports:
```python
import asyncio

from webbee.account import Account
from webbee.home import (_mask_email, _notify_state_from, _pick_session_slot,
                         fill_home, is_stale)
from webbee.repl import _home_input, _home_target_workspace, _schedule_home_refill
from webbee.slots import SessionSlot, SlotManager, WorkspaceResources
from webbee.wallet import Wallet
```
Replace `FakePane`, `_home_slot`, `_session_slot` helpers:
```python
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
```
Delete every `render_*`/`render_skeleton`/`WELCOME_HINT` test. Add `_notify_state_from` + `fill_home` tests:
```python
def test_notify_state_from_maps_mirror():
    assert _notify_state_from({}) == "off"
    assert _notify_state_from({"enabled": False}) == "off"
    assert _notify_state_from({"enabled": True, "mirror": ["telegram"]}) == "tg"
    assert _notify_state_from({"enabled": True, "mirror": ["panel"]}) == "panel"
    assert _notify_state_from({"enabled": True, "mirror": ["telegram", "panel"]}) == "both"


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
```
- [ ] **Step 2: Run it, watch it fail.** `python -m pytest tests/test_home.py -q` → `ImportError: cannot import name '_notify_state_from'` (and render_* import failures if not yet removed).
- [ ] **Step 3: Minimal impl.** In `src/webbee/home.py`: delete the module constants `HOME_HINT`/`_SECTIONS`, the imports `from webbee.banner_art import WEBBEE_CODE` and `from webbee.render import _ACCENT, _BEE`, `from rich.align import Align`, `from rich.text import Text`, and the functions `_render_header`, `_render_hint`, `render_skeleton`, `render_identity`, `render_slots_tile`, `render_repo_tile`, `render_system_tile`. Keep `_mask_email`, `is_stale`, `_pick_session_slot`. Add `_notify_state_from` and replace `fill_home`:
```python
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
    """A non-PII human label for a device/session row (never a raw IP)."""
    for k in ("device", "user_agent", "client", "name", "location"):
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
```
Keep `import asyncio`, `import os`, `import time` at the top of home.py (all still used).
- [ ] **Step 4: Confirm no stale references.** `grep -rn "render_skeleton\|render_identity\|render_slots_tile\|render_repo_tile\|render_system_tile\|HOME_HINT" src/ tests/` → only comment-free hits are gone (docstrings referencing "skeleton" as prose are fine).
- [ ] **Step 5: Run it, watch it pass.** `python -m pytest tests/test_home.py -q` → all pass; `python -m py_compile src/webbee/home.py`.
- [ ] **Step 6: Commit.** `git add src/webbee/home.py tests/test_home.py && git commit -m "Rewire fill_home to populate HomeData; drop per-tile renderers"`.

---

### Task 8: `repl.py` wiring — build `HomeView` at boot, actions, helpers, `wallet_fetcher`, new-tab mode seed

**Files**
- Modify `src/webbee/repl.py` (dock boot block ~1215-1220; `home_fill_kwargs` ~743-745; `_open_new_tab` ~1191-1192; `home_input=` wiring ~1258-1263)
- Modify `tests/test_home.py` (extend: `_open_new_tab`/`_home_input` honor new-tab mode — optional light test; the heavy wiring is live-smoke)

**Interfaces**
- Consumes: `HomeView`, `HomeActions`, `SECURITY_DOCS_URL` (home_view); `wallet.fetch_wallet`; `newtab_mode.load_newtab_mode`/`save_newtab_mode`; `urlopen.open_url`; `home._pick_session_slot`; `remote.set_remote`/`describe`; existing `_open_new_tab`, `set_slot_mode`, `_cancel_slot`, `close_at`, `ui_hooks`, `state`, `cfg`, `token_provider`.
- Produces: a Home slot whose `.pane` is a wired `HomeView`; `home_fill_kwargs` gains `wallet_fetcher`; `state["new_tab_mode"]` seeds new tabs.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_home.py` (light, deterministic — exercises the new-tab-mode seam that Task 8 threads through `_open_new_tab`/`_home_input`):
```python
def test_home_input_uses_new_tab_mode_when_provided():
    slots = SlotManager()
    slots.add(_home_slot(workspace="/cwd"))
    seen = {}

    async def fake_make_session_slot(cfg, tp, ws, mode, *, resources, shared_client,
                                     agent_factory, intel_factory, shadow_factory, first):
        seen["mode"] = mode
        s = _session_slot(workspace=ws)
        class _E:
            echoed = []
            def user_echo(self, t): self.echoed.append(t)
        s.sink = _E()
        return s

    async def fake_run_turn(slot, text):
        pass

    import webbee.repl as repl_mod
    orig = repl_mod._make_session_slot
    repl_mod._make_session_slot = fake_make_session_slot
    try:
        asyncio.run(_home_input(
            "go", slots=slots, cfg=None, token_provider=None, mode="plan",
            resources=WorkspaceResources(), shared_client=None, agent_factory=None,
            intel_factory=None, shadow_factory=None, workspace="/cwd",
            ui_hooks={}, run_turn=fake_run_turn))
    finally:
        repl_mod._make_session_slot = orig
    assert seen["mode"] == "plan"    # _home_input threads the caller-chosen mode through unchanged
```
(This confirms `_home_input` passes `mode` verbatim; Task 8 supplies `state["new_tab_mode"]` as that `mode` at the call site.)
- [ ] **Step 2: Run it, watch it fail (or pass-as-baseline).** `python -m pytest tests/test_home.py::test_home_input_uses_new_tab_mode_when_provided -q` — if `_home_input` already forwards `mode`, it passes; treat as the regression pin for Step 3's call-site change.
- [ ] **Step 3: Wire repl.** (a) In `home_fill_kwargs` (~743) add the wallet fetcher:
```python
    from webbee.wallet import fetch_wallet as _wallet_fetcher
    home_fill_kwargs = dict(cfg=cfg, token_provider=token_provider, slots=slots,
                            account_fetcher=account_fetcher, sessions_client=sessions_client,
                            resources=resources, version=__version__,
                            wallet_fetcher=_wallet_fetcher)
```
(b) In `_open_new_tab` (~1191) change the mode argument from `mode` to the seeded default:
```python
        new_slot = await _make_session_slot(
            cfg, token_provider, ws, state.get("new_tab_mode") or mode, resources=resources,
            shared_client=shared_client, agent_factory=agent_factory,
            intel_factory=intel_factory, shadow_factory=shadow_factory,
            first=False)
```
(c) In the dock boot block, replace the Home-slot construction (currently `home_pane = tui.OutputPane(...)` then `SessionSlot(kind="home", ..., pane=home_pane, ...)`, ~1217-1220) with the wired HomeView. Insert BEFORE it (still inside the `with contextlib.redirect_stderr(...)` block, after `width, _height = get_size(None)`):
```python
                from prompt_toolkit.application import get_app
                from webbee import newtab_mode, urlopen
                from webbee import remote as _remote_mod
                from webbee.home import _pick_session_slot
                from webbee.home_view import HomeActions, HomeView, SECURITY_DOCS_URL

                state["new_tab_mode"] = newtab_mode.load_newtab_mode() or mode

                def _home_set_notify(arg: str) -> None:
                    picked = _pick_session_slot(slots)
                    sid = getattr(getattr(picked, "agent", None), "session_id", "") if picked else ""
                    if not sid:
                        return
                    async def _do():
                        try:
                            st = await _remote_mod.set_remote(cfg, token_provider, sid, arg)
                            home_view.data.notify_state = arg
                            home_view.data.remote_desc = _remote_mod.describe(st)
                        except Exception:
                            pass
                        home_view.notify()
                    get_app().create_background_task(_do())

                def _set_new_tab_mode(m: str) -> None:
                    state["new_tab_mode"] = m
                    newtab_mode.save_newtab_mode(m)
                    home_view.data.new_tab_mode = m
                    home_view.notify()

                def _set_tab_mode(idx: int, m: str) -> None:
                    if 0 <= idx < len(slots.slots):
                        set_slot_mode(slots.slots[idx], m)
                        get_app().invalidate()

                def _home_close_tab(idx: int) -> None:
                    if close_at(slots, idx, _cancel_slot):
                        get_app().invalidate()

                def _home_switch(idx: int) -> None:
                    ui_hooks.get("switch", slots.switch)(idx)

                def _home_top_up() -> None:
                    url = urlopen.open_url(f"{cfg.panel_url}/billing")
                    home_view.data.notice = f"top up at {url}"
                    home_view.notify()

                def _home_security_docs() -> None:
                    url = urlopen.open_url(SECURITY_DOCS_URL)
                    home_view.data.notice = f"security & privacy: {url}"
                    home_view.notify()

                home_view = HomeView(slots=slots, width=width, actions=HomeActions(
                    new_session=lambda: get_app().create_background_task(_open_new_tab()),
                    open_recent=lambda p: get_app().create_background_task(_open_new_tab(p)),
                    switch_tab=_home_switch,
                    close_tab=_home_close_tab,
                    set_tab_mode=_set_tab_mode,
                    set_notify=_home_set_notify,
                    set_new_tab_mode=_set_new_tab_mode,
                    top_up=_home_top_up,
                    open_security_docs=_home_security_docs,
                ))
                home_view.data.new_tab_mode = state["new_tab_mode"]
                home_slot = SessionSlot(kind="home", workspace=workspace, label="Home",
                                        pane=home_view, sink=None, agent=None)
                slots.add(home_slot)
```
Remove the old `home_pane = tui.OutputPane(width=width)` + its `home_slot = SessionSlot(...)` + `slots.add(home_slot)` lines it replaces. Ensure `close_at` is imported in repl (it's used via `webbee.slots.close_at`; add `from webbee.slots import close_at` near the top imports if not already present — verify with grep).
(d) In the `home_input=` wiring (~1258) change `mode=mode` to `mode=(state.get("new_tab_mode") or mode)`:
```python
                        home_input=lambda text: _home_input(
                            text, slots=slots, cfg=cfg, token_provider=token_provider,
                            mode=(state.get("new_tab_mode") or mode),
                            resources=resources, shared_client=shared_client, agent_factory=agent_factory,
                            intel_factory=intel_factory, shadow_factory=shadow_factory,
                            workspace=workspace, ui_hooks=ui_hooks, run_turn=_run_turn,
                            spawn_poller=_spawn_slot_poller),
```
- [ ] **Step 4: Run tests.** `python -m pytest tests/test_home.py tests/test_repl.py -q` → all pass; `python -m py_compile src/webbee/repl.py`.
- [ ] **Step 5: LIVE-ONLY note.** The full boot wiring (HomeView becomes the visible pane; clicking tiles fires the real seams; notifications hit the live gateway) is smoke-verified in a real tty in the final task, not headless.
- [ ] **Step 6: Commit.** `git add src/webbee/repl.py tests/test_home.py && git commit -m "Wire HomeView into repl boot with actions, wallet fetch, new-tab mode"`.

---

### Task 9: `tui.py` — bee-yellow "+", `home.*` styles (hoisted `_STYLE_DICT`), `c-t` → new tab

**Files**
- Modify `src/webbee/tui.py` (hoist Style dict to module-level `_STYLE_DICT` ~1145-1173; change `tab.new` ~1154; add `home.*` classes; `run_session` uses `Style.from_dict(_STYLE_DICT)`; change `c-t` body ~770-776)
- Modify `tests/test_tui.py` (add style + c-t regression tests)
- Modify `tests/test_tabs.py` (extend: "+" chip uses `TAB_STYLE_NEW` and fires `on_new`)
- Modify `tests/test_repl.py` (the 3 `test_home_active_*` tests reach Home via `\x14` Ctrl-T today — Ctrl-T now OPENS A TAB, so switch them to Alt+0 `\x1b0`, the existing slot-0 switch; see Step 3(c))

**Interfaces**
- Produces: module-level `_STYLE_DICT: dict[str, str]` with `"tab.new": "#e8a317 bold"` and `home.*` classes; `c-t` binding calls `_new_tab_click()`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_tui.py` add:
```python
def test_tab_new_style_is_bee_yellow():
    from webbee.tui import _STYLE_DICT
    assert _STYLE_DICT["tab.new"].startswith("#e8a317")


def test_home_style_classes_present():
    from webbee.tui import _STYLE_DICT
    for cls in ("home.header", "home.value", "home.item", "home.dim",
                "home.disabled", "home.focus", "home.hint"):
        assert cls in _STYLE_DICT


def test_ctrl_t_binding_opens_new_tab_not_home():
    import inspect

    from webbee import tui
    src = inspect.getsource(tui.run_session)
    i = src.index('kb.add("c-t")')
    body = src[i:i + 500]
    assert "_new_tab_click()" in body
    assert "_switch_to(0)" not in body
```
In `tests/test_tabs.py` add:
```python
def test_new_chip_uses_tab_new_style_and_fires_on_new():
    slots = _mk_slots(("alpha", _FakeSink()), active_idx=1)
    fired = []
    frags = tab_fragments(slots, on_switch=_noop, on_close=_noop, on_new=lambda: fired.append(1))
    pad_before, glyph, pad_after = _new_chip_pieces(frags)
    assert glyph[1] == "+" and glyph[0] == TAB_STYLE_NEW and len(glyph) == 3
    assert _up(glyph[2]) is None and fired == [1]
```
- [ ] **Step 2: Run them, watch them fail.** `python -m pytest tests/test_tui.py -k "tab_new or home_style or ctrl_t" tests/test_tabs.py::test_new_chip_uses_tab_new_style_and_fires_on_new -q` → `ImportError: cannot import name '_STYLE_DICT'` and the c-t assertion fails (`_switch_to(0)` still present).
- [ ] **Step 3: Impl.** (a) Hoist the Style dict: move the dict literal currently inside `run_session` (lines ~1145-1173) to module scope near the top of `tui.py` (after `scrub_mouse_residue`, ~line 55) as `_STYLE_DICT = { ... }`. Change `"tab.new"` value to `"#e8a317 bold"` and append the Home classes:
```python
_STYLE_DICT = {
    "frame.border": "#5f5f5f",
    "prompt": "#00afd7 bold",
    "tabbar": "bg:#262626",
    "tab": "#9e9e9e",
    "tab.active": "bg:#e8a317 #1c1c1c bold",
    "tab.alert": "#e8a317 bold",
    "tab.close": "#9e9e9e",
    "tab.close.active": "bg:#e8a317 #1c1c1c",
    "tab.new": "#e8a317 bold",            # 0.3.26: bee-yellow + prominent (was #6f6f6f)
    "tab.sep": "#3a3a3a",
    "tb.dim": "#8a8a8a",
    "tb.spin": "#e8a317 bold",
    "tb.working": "#e8a317",
    "tb.action": "#00afd7",
    "tb.consent": "#e8a317 bold",
    "tb.mode.default": "#00afd7",
    "tb.mode.plan": "#af87ff",
    "tb.mode.autopilot": "#e8a317 bold",
    "qp.header": "#e8a317 bold",
    "qp.item": "#8a8a8a italic",
    "qp.last": "#e8a317",
    "qp.remote": "#af87ff italic",
    "tp.header": "#e8a317 bold",
    "tp.done": "#5faf5f",
    "tp.done.text": "#8a8a8a strike",
    "tp.now": "#e8a317 bold",
    "tp.item": "#8a8a8a",
    # W5 interactive Home dashboard
    "home.header": "#e8a317 bold",
    "home.value": "#ffffff bold",
    "home.item": "#00afd7",
    "home.dim": "#8a8a8a",
    "home.disabled": "#5f5f5f",
    "home.focus": "bg:#e8a317 #1c1c1c bold",
    "home.hint": "#00afd7",
}
```
Then in `run_session` replace the inline `style = Style.from_dict({ ... })` with `style = Style.from_dict(_STYLE_DICT)`.
(b) Change the `c-t` binding (~770-776) body:
```python
    @kb.add("c-t")
    def _new_tab_key(event):
        # 0.3.26: Ctrl-T opens a NEW tab (the browser gesture), via the exact
        # seam the tab bar's + chip uses (`_new_tab_click` -> on_new ->
        # repl._open_new_tab). Home stays reachable by clicking its ◆ chip or
        # Alt+1-style switch (footer legend reminds muscle-memory users).
        _new_tab_click()
```
(`_new_tab_click` is defined at ~1078; it's in scope where `c-t` is registered because both are inside `run_session` and the binding body runs after full setup.)
(c) **Fix the 3 Home-active dock tests for the new Ctrl-T.** In `tests/test_repl.py`, the tests `test_home_active_help_renders_into_the_home_pane`, `test_home_active_steps_yields_open_a_tab_note`, and `test_home_active_tabs_lists_tabs` each do `pipe.send_text("\x14")  # Ctrl-T -- jump to Home`. Ctrl-T now opens a NEW tab, so this no longer lands on Home. Replace that one line in each of the 3 tests with the existing slot-0 switch (Alt+0, mirroring the `\x1b1`/`\x1b2` Alt-switch already used elsewhere in this file):
```python
                pipe.send_text("\x1b0")            # Alt+0 -- switch to Home (slot 0)
```
Do NOT change anything else in those tests — the `created_panes[0].dump()` assertions still hold (Home's composed OutputPane is `created_panes[0]`).
- [ ] **Step 4: Run them, watch them pass.** `python -m pytest tests/test_tui.py tests/test_tabs.py -q` → all pass; then the 3 fixed tests: `python -m pytest tests/test_repl.py -k "home_active" -q` → `3 passed`; then the FULL suite `python -m pytest -q` → all pass (confirms the c-t change broke nothing else). `python -m py_compile src/webbee/tui.py`.
- [ ] **Step 5: Commit.** `git add src/webbee/tui.py tests/test_tui.py tests/test_tabs.py tests/test_repl.py && git commit -m "Bee-yellow +, Home style classes, Ctrl-T opens a new tab"`.

---

### Task 10: `tui.py` — Home-scoped key bindings (↑↓/Tab/↵/←→) + `?1003` hover scoping

**Files**
- Modify `src/webbee/tui.py` (`_enter` ~701; `_step_up`/`_step_down` ~823-830; `_cycle` s-tab ~761-764; add `tab`/`left`/`right` filtered bindings; add `_sync_hover_mode` near `_switch_to` ~986; call it in `_switch_to` and the local `_ticker` ~1194)
- Modify `tests/test_tui.py` (source-structure regression tests; behavior is live-verified)

**Interfaces**
- Consumes: `_a()` (active slot), `buf` (shared input Buffer), `HomeView` methods (`move_focus`/`focus_next`/`focus_prev`/`activate_focused`/`seg_left`/`seg_right`) via `_a().pane`; `Condition` (already imported, used at ~790); `get_app_or_none`.

Home nav is gated on **active-slot `kind == "home"` AND empty input** so typing a task on Home (which starts a session) and editing still work; when the gate is False, default buffer behavior (history recall, cursor move) is untouched. `?1003` is enabled only while Home is active and restored to `?1002` on leave; teardown (`configure_mouse_modes._disable`, tui.py:84-90) already emits `?1003l`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_tui.py` add source-structure pins (headless key-routing through the full Application is not unit-testable here — tui's own doctrine: "pure helpers unit-tested; the Application is TTY/headless-smoke verified"):
```python
def test_home_nav_bindings_gated_on_home_and_empty_input():
    import inspect

    from webbee import tui
    src = inspect.getsource(tui.run_session)
    # left/right/tab bound with a Home+empty gate
    assert 'kb.add("left"' in src and 'kb.add("right"' in src and 'kb.add("tab"' in src
    assert 'kind == "home"' in src
    # the Home branch calls the view's focus/segment methods
    for m in ("activate_focused", "move_focus", "seg_left", "seg_right", "focus_prev"):
        assert m in src


def test_hover_scoping_present_and_home_only():
    import inspect

    from webbee import tui
    src = inspect.getsource(tui.run_session)
    assert "?1003h" in src and "?1003l" in src
    assert "_sync_hover_mode" in src
```
- [ ] **Step 2: Run them, watch them fail.** `python -m pytest tests/test_tui.py -k "home_nav or hover_scoping" -q` → assertions fail (methods/sequences absent).
- [ ] **Step 3: Impl.**
(a) In `_enter` (~701), right after the draft-clear lines (`slot.draft = ""` / `slot.draft_cursor = 0`, ~715) and BEFORE the consent block, insert:
```python
        if slot.kind == "home" and not text.strip():
            # Empty Enter on the interactive Home activates the focused item.
            slot.pane.activate_focused()
            return
```
(b) Modify `_step_up` / `_step_down` to delegate on Home+empty:
```python
    @kb.add("up")
    def _step_up(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.move_focus(-1)
            return
        _arrow_up_action(event, buf, sel, _nav_count(), _busy_live(), slot.pending, slot.pulled)

    @kb.add("down")
    def _step_down(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.move_focus(1)
            return
        _arrow_down_action(event, buf, sel, _nav_count(), _busy_live())
```
(c) Modify the s-tab `_cycle` binding to move focus backward on Home:
```python
    @kb.add("s-tab")
    def _cycle(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.focus_prev()
            event.app.invalidate()
            return
        on_cycle()
        event.app.invalidate()
```
(d) Add Tab / Left / Right bindings, gated (place near the other bindings, after `_step_clear`):
```python
    _home_nav = Condition(lambda: _a().kind == "home" and not buf.text)

    @kb.add("tab", filter=_home_nav)
    def _home_focus_next(event):
        _a().pane.focus_next()

    @kb.add("left", filter=_home_nav)
    def _home_seg_left(event):
        _a().pane.seg_left()

    @kb.add("right", filter=_home_nav)
    def _home_seg_right(event):
        _a().pane.seg_right()
```
(e) Hover scoping. Define `_sync_hover_mode` immediately before `_switch_to` (~986):
```python
    _hover_on = {"v": None}

    def _sync_hover_mode() -> None:
        # ?1003 (any-event mouse = hover) ONLY while Home is active; restore
        # ?1002 (button-event) on leave. Idempotent: writes only on a state
        # change. Teardown's own ?1003l (configure_mouse_modes._disable) is
        # the belt-and-braces cleanup on exit.
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
        if app is None:
            return
        want = (_a().kind == "home")
        if _hover_on["v"] == want:
            return
        out = app.output
        if not hasattr(out, "write_raw"):
            _hover_on["v"] = want
            return
        try:
            if want:
                out.write_raw("\x1b[?1003h")
            else:
                out.write_raw("\x1b[?1003l")
                out.write_raw("\x1b[?1002h")
            out.flush()
        except Exception:
            pass
        _hover_on["v"] = want
```
Call it at the end of `_switch_to` (after `get_app().invalidate()`, inside the `if slots.switch(idx):` block):
```python
            if on_switch is not None:
                on_switch(idx)
            get_app().invalidate()
            _sync_hover_mode()
```
And in the local `_ticker` loop (~1202) add a call each iteration so the initial state (boot may land on Home) applies once the renderer is up:
```python
        while True:
            await asyncio.sleep(0.25)
            _sync_hover_mode()
            _tick_once(slots, app, _busy_live)
```
- [ ] **Step 4: Run them, watch them pass.** `python -m pytest tests/test_tui.py -q` → all pass; `python -m py_compile src/webbee/tui.py`.
- [ ] **Step 5: LIVE-ONLY verification (record, do not skip).** In a real tty: switch to Home → confirm ↑↓/Tab move the focus highlight, ←→ change the mode/notifications segments, ↵ activates, mouse hover highlights the item under the cursor with no input residue (no stray `<35;..M` / `[I` in the box), and switching AWAY from Home restores `?1002` (no hover flood during a live session). If input corruption appears, fall back to focus-ring + click highlight (the highlight model is shared — no code path removed).
- [ ] **Step 6: Commit.** `git add src/webbee/tui.py tests/test_tui.py && git commit -m "Home-scoped key bindings and hover (?1003) scoped to Home"`.

---

### Task 11: Trust/Security tile — confirm copy against the voice bible + docs URL (claims doctrine)

**Files**
- Modify `src/webbee/home_view.py` ONLY IF the review changes the strings (`SECURITY_LINES` / `SECURITY_DOCS_URL`)
- Modify `tests/test_home_view.py` if the exact strings change (the assertions in Task 5/6 reference them)

**Interfaces:** none new — this is a copy-review gate, not code. Default constants already ship (Task 5). This task confirms/edits them.

- [ ] **Step 1: Read the sources of truth.** Read `~/Nextcloud/MCP-Marketing-Imperal/07-voice-and-messaging.md` (voice bible + Locus-of-Authority claims doctrine) and the `feedback_icnli_claims_locus_of_authority` memory. Confirm each line maps to a real, enforced property: PII masking (`EXPOSE_PII_TO_LLM=false`, per-surface), consent-before-execution (elicit-first, policy-gated), TLS to `auth.imperal.io`. Reject any line not backed by a verifiable property (no "we never store your data", no compliance badges).
- [ ] **Step 2: Confirm the docs URL is live.** Verify the canonical security/privacy page URL on `docs.imperal.io` (Fumadocs shape `/en/<section>/<page>/`). If `https://docs.imperal.io/en/security/overview/` 404s, find the real one; update `SECURITY_DOCS_URL`. Cross-check against `reference_docs_imperal_io_link_integrity`.
- [ ] **Step 3: Reconcile with the existing welcome copy.** The splash already ships claims-disciplined copy (`render.WELCOME_PRIVACY` = "Your work stays yours — never sold, never training data.", `WELCOME_PRIVACY_DETAIL` = "PII is masked before it reaches the model."). Keep the Home tile CONSISTENT with that wording/voice; do not introduce a stronger claim than the splash makes.
- [ ] **Step 4: Apply + retest.** If any string changed, update `SECURITY_LINES`/`SECURITY_DOCS_URL` in `home_view.py` and the matching assertion strings in `tests/test_home_view.py`, then `python -m pytest tests/test_home_view.py -q`.
- [ ] **Step 5: Commit (only if changed).** `git add src/webbee/home_view.py tests/test_home_view.py && git commit -m "Confirm Trust/Security tile copy against voice bible and docs"`.

---

### Task 12: Finalize — version 0.3.26, CHANGELOG, Cyrillic grep, full suite, py_compile

**Files**
- Modify `pyproject.toml` (line 3)
- Modify `src/webbee/__init__.py` (line 1)
- Modify `CHANGELOG.md` (new top section)

- [ ] **Step 1: Bump version.** `pyproject.toml`: `version = "0.3.25"` → `version = "0.3.26"`. `src/webbee/__init__.py`: `__version__ = "0.3.25"` → `__version__ = "0.3.26"`.
- [ ] **Step 2: Verify the version test.** `python -m pytest tests/test_version.py tests/test_packaging.py -q` → pass (they pin `__version__` == pyproject).
- [ ] **Step 3: CHANGELOG.** Add a `## 0.3.26` section at the top of `CHANGELOG.md`, user-facing English prose, e.g.:
```markdown
## 0.3.26

The Home tab is now an interactive dashboard — a little website inside your
terminal. Everything is clickable and keyboard-navigable, with a highlight
that follows your focus and the mouse.

- Home shows your account and plan, your credits balance, your open tabs
  (with each tab's mode, a close button, and how much that session has
  spent), your recent repositories (one click reopens one in a new tab),
  the devices you're signed in on, and a small Settings panel.
- Settings you can change right from Home: the mode new tabs open in,
  where a running session sends notifications, and a Top-up credits button.
- A Trust & security panel explains, in plain terms, how your data is
  handled — with a link to the full security and privacy docs.
- The new-tab "+" button is now bee-yellow and easy to spot.
- Ctrl+T now opens a new tab, like a browser (it used to jump to Home).
  Home is still one click away on its own tab.
```
- [ ] **Step 4: Cyrillic grep (English-only rule).** `grep -rnP '[\x{0400}-\x{04FF}]' src/webbee/home_view.py src/webbee/wallet.py src/webbee/newtab_mode.py src/webbee/urlopen.py src/webbee/home.py src/webbee/tui.py src/webbee/repl.py src/webbee/slots.py` → **no output** (any hit is a blocker; reword to English).
- [ ] **Step 5: py_compile every touched file.** `python -m py_compile src/webbee/wallet.py src/webbee/newtab_mode.py src/webbee/urlopen.py src/webbee/home_view.py src/webbee/home.py src/webbee/slots.py src/webbee/tui.py src/webbee/repl.py src/webbee/__init__.py` → exit 0.
- [ ] **Step 6: Full suite green.** `python -m pytest -q` → all pass (0 failures). Investigate and fix any regression before proceeding.
- [ ] **Step 7: LIVE smoke (real tty).** Launch `webbee` in a real terminal: Home paints the dashboard; tiles are clickable and keyboard-navigable; the "+" is yellow; Ctrl+T opens a new tab; hover works on Home only with no input residue; switching away restores normal mouse behavior. Confirm no traceback in the dock stderr log.
- [ ] **Step 8: Commit.** `git add pyproject.toml src/webbee/__init__.py CHANGELOG.md && git commit -m "Release 0.3.26 — interactive Home dashboard"`.
