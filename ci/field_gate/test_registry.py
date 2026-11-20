"""Every field-report item has a scenario; every scenario names real items."""
from ci.field_gate import (scenarios_account, scenarios_code, scenarios_contract,  # noqa: F401  (registration)
                           scenarios_long, scenarios_wait)
from ci.field_gate.registry import ITEMS, SCENARIOS


def test_every_report_item_has_scenario():
    covered = {i for s in SCENARIOS for i in s.items}
    assert sorted(set(ITEMS) - covered) == []


def test_scenario_ids_unique():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))


def test_the_refreshed_login_goes_back_whenever_it_differs(tmp_path):
    # 2026-11-20: a gate stopped mid-run never returned the token it had refreshed; the test user was logged out
    from ci.field_gate import run
    gate = tmp_path / "gate" / "credentials.json"
    gate.parent.mkdir()
    gate.write_text("old")
    home = tmp_path / "home"
    run._login_back(home, gate)                         # the run wrote no login: the gate's stays
    assert gate.read_text() == "old"
    (home / ".imperal").mkdir(parents=True)
    (home / ".imperal" / "credentials.json").write_text("refreshed")
    run._login_back(home, gate)
    assert gate.read_text() == "refreshed" and not list(gate.parent.glob("*.gate-tmp"))
    run._login_back(home, None)                         # a run without an account has nothing to return
