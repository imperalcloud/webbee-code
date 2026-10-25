"""The kernel's own probes (evidence / reality) are free: never shown as a priced step (owner 2026-10-25)."""
from webbee.pricing_constants import estimate_action_cost


def test_a_kernel_probe_is_shown_free():
    assert estimate_action_cost("bash", ": imperal-evidence-probe; for f in a.py; do cksum -- \"$f\"; done") == 0.0
    assert estimate_action_cost("bash", ": imperal-reality-probe-git; git -C . rev-parse HEAD") == 0.0
    assert estimate_action_cost("bash", "ls -la") == estimate_action_cost("bash")
