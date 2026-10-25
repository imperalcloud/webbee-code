"""Tool pricing constants and lookup for Webbee actions."""

DEFAULT_ACTION_PRICES = {
    # Local workspace operations (always 0.00 cr)
    "read_file": 0.0,
    "write_file": 0.0,
    "edit_file": 0.0,
    "multi_edit": 0.0,
    "diff": 0.0,
    "rollback": 0.0,
    "checkpoint": 0.0,
    "list_dir": 0.0,
    "grep": 0.0,
    "glob": 0.0,
    "search_code": 0.0,
    "graph_slice": 0.0,
    "impact_of_change": 0.0,
    "orient": 0.0,
    "repo_profile": 0.0,
    "view_image": 0.0,
    
    # Server / remote / external connector operations
    "conn-ssh": 0.15,
    "bash": 0.05,
    "shell": 0.05,
    "web_search": 0.05,
    "read_url": 0.02,
    "read_url_rendered": 0.10,
    "wordpress-hub": 0.10,
    "stripe-connector": 0.15,
    "google-analytics-bluebee": 0.10,
    "mail": 0.05,
    "tasks": 0.02,
    "docusign-connector": 0.20,
    "plaid-connector": 0.25,
    "proxmox-connector": 0.15,
    "se-ranking-connector": 0.15,
    "bing-webmaster-connector": 0.05,
    "google-search-console-connector": 0.05,
    "google-drive-connector": 0.05,
    "telegram-publisher-extension": 0.05,
    "x-connector": 0.10,
    "thoughts": 0.01,
    "memory-index": 0.01,
}

# the kernel's own read-only probes (evidence / reality) are the kernel's work, never the user's priced step
_KERNEL_PROBES = (": imperal-evidence-probe;", ": imperal-reality-probe")


def estimate_action_cost(tool_name: str, arg: str = "") -> float:
    """Estimate credit cost for an action by tool name (a kernel probe is free)."""
    if not tool_name or any(m in str(arg or "") for m in _KERNEL_PROBES):
        return 0.0
    t = tool_name.strip()
    if t in DEFAULT_ACTION_PRICES:
        return DEFAULT_ACTION_PRICES[t]
    if "·" in t:
        app, op = t.split("·", 1)
        if app in DEFAULT_ACTION_PRICES:
            return DEFAULT_ACTION_PRICES[app]
        if op in DEFAULT_ACTION_PRICES:
            return DEFAULT_ACTION_PRICES[op]
    elif "__" in t:
        app, op = t.split("__", 1)
        if app in DEFAULT_ACTION_PRICES:
            return DEFAULT_ACTION_PRICES[app]
        if op in DEFAULT_ACTION_PRICES:
            return DEFAULT_ACTION_PRICES[op]
    if any(k in t for k in ("ssh", "remote", "http", "api", "conn")):
        return 0.15
    return 0.0
