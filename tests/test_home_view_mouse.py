import pytest
from webbee.slots import SlotManager, SessionSlot
from webbee.home_view import HomeView
from webbee.output_pane import OutputPane

def test_home_view_has_forward_mouse():
    slots = SlotManager()
    hv = HomeView(slots=slots, actions={})
    assert hasattr(hv, "forward_mouse"), "HomeView must have forward_mouse to prevent AttributeError on tab switch or mouse events"
    # Should accept ev and clamp without crashing
    hv.forward_mouse(None, clamp="top")
