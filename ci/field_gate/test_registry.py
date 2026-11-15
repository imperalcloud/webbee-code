"""Every field-report item has a scenario; every scenario names real items."""
from ci.field_gate import scenarios_account, scenarios_code, scenarios_contract  # noqa: F401  (registration)
from ci.field_gate.registry import ITEMS, SCENARIOS


def test_every_report_item_has_scenario():
    covered = {i for s in SCENARIOS for i in s.items}
    assert sorted(set(ITEMS) - covered) == []


def test_scenario_ids_unique():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
