import pytest
from webbee.slots import SlotManager, SessionSlot
from webbee.repl import set_slot_tier
from webbee.tier_store import load_tier, save_tier

def test_set_slot_tier_propagates_and_invalidates(tmp_path):
    class DummyAgent:
        def __init__(self):
            self.model_tier = ""
            self.invalidated = False
        def invalidate_cache(self):
            self.invalidated = True

    workspace = str(tmp_path)
    slot = SessionSlot(kind="session", workspace=workspace, label="TestSlot", pane=None, sink=None, agent=DummyAgent())
    
    set_slot_tier(slot, "ultrasmart")
    assert slot.model_tier == "ultrasmart"
    assert slot.agent.model_tier == "ultrasmart"
    assert load_tier(workspace) == "ultrasmart"
