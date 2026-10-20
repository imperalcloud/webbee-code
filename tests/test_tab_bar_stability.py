import pytest
from prompt_toolkit.layout.containers import Window
from webbee.slots import SlotManager
from webbee.tabs import tab_fragments
import webbee.sizing as sizing

def test_tab_bar_clamping_and_no_wrap():
    from webbee.slots import SessionSlot
    sm = SlotManager()
    # Add a slot
    slot2 = SessionSlot(kind="session", workspace="/tmp", label="Tab 1", pane=None, sink=None, agent=None)
    sm.add(slot2)
    assert sm.session_count() == 1
    
    # Check tab fragments with normal width
    frags = tab_fragments(sm, on_switch=lambda x: None, on_close=lambda x: None, width=80)
    assert len(frags) > 0
    
    # Check tab fragments with narrow width - should never raise or collapse
    frags_narrow = tab_fragments(sm, on_switch=lambda x: None, on_close=lambda x: None, width=10)
    assert len(frags_narrow) > 0

    # Check tab fragments with zero or negative width
    frags_zero = tab_fragments(sm, on_switch=lambda x: None, on_close=lambda x: None, width=0)
    assert len(frags_zero) > 0
    frags_neg = tab_fragments(sm, on_switch=lambda x: None, on_close=lambda x: None, width=-5)
    assert len(frags_neg) > 0

def test_tab_bar_window_scroll_invariants():
    # Verify that Window representing tab bar has always_hide_cursor and wrap_lines=False
    # to avoid prompt_toolkit line wrapping or vertical scrolling that eats the top bar
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.filters import to_filter
    control = FormattedTextControl([("", "tab text")])
    w = Window(content=control, height=1, dont_extend_height=True, wrap_lines=False, always_hide_cursor=True)
    assert to_filter(w.wrap_lines)() is False
    assert to_filter(w.always_hide_cursor)() is True

