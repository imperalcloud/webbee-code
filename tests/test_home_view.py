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
