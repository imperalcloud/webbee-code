"""Model-tier persistence per-repo (webbee-code-model-tier-slash-command-v1):
remembers the chosen coding brain tier (webismart/supersmart/ultrasmart) across
process restarts by writing a tiny marker file under
``~/.cache/webbee/tier-{repo_key}`` -- the exact same repo identity + cache
directory convention `mode_store.py` already uses, sibling file, sibling
logic, deliberately NOT unified into one store: mode and tier are two
independent per-repo choices with different valid-value sets and different
(zero) cross-feature coupling.

Fail-soft in both directions, by design, same rationale as mode_store:
  * `load_tier` -- a missing file, an unreadable dir, or corrupt/garbage/
    since-retired tier name all degrade to None. The caller's own baseline
    (unset -> server-side admin default) is always the fallback, so a bad
    cache is exactly as safe as no cache at all.
  * `save_tier` -- a write failure (read-only home, disk full, no
    permission) is silently dropped: losing the memory is a far smaller
    problem than crashing the terminal over a nice-to-have.
"""
from __future__ import annotations

import os

from webbee.repo import compute_repo_key, find_repo_root

_CACHE_DIR = os.path.expanduser("~/.cache/webbee")   # test seam: monkeypatch this name

# Single source of truth for valid tier ids -- MUST mirror the kernel's
# imperal_kernel.llm.model_tiers.MODEL_TIERS (server is the real authority;
# this tuple only gates what the CLIENT will ever persist/offer locally, so
# a future 4th tier is a one-line change here + one line server-side, never
# a protocol change).
TIERS: tuple[str, ...] = ("webismart", "supersmart", "ultrasmart")

# repo_key is derived from `git remote get-url origin` (compute_repo_key,
# timeout=5) -- cached per workspace for the process lifetime, same as
# mode_store's _KEY_CACHE (identical rationale: never re-shell out on every
# keystroke/command).
_KEY_CACHE: dict[str, str] = {}


def _repo_key_for(workspace: str) -> str:
    key = _KEY_CACHE.get(workspace)
    if key is None:
        key = compute_repo_key(find_repo_root(workspace))
        _KEY_CACHE[workspace] = key
    return key


def _path_for(workspace: str) -> str:
    return os.path.join(_CACHE_DIR, f"tier-{_repo_key_for(workspace)}")


def load_tier(workspace: str) -> "str | None":
    """The remembered tier for `workspace`'s repo, or None on no file / ANY
    error / an unrecognised value -- never raises. An unrecognised value
    (e.g. a tier retired in a later release) is treated as if nothing were
    ever saved, not as a broken state."""
    try:
        with open(_path_for(workspace), "r", encoding="utf-8") as f:
            tier = f.read().strip()
        return tier if tier in TIERS else None
    except Exception:
        return None


def save_tier(workspace: str, tier: str) -> None:
    """Remember `tier` for `workspace`'s repo -- `tier=""` means "forget my
    choice, resume the server admin default" and REMOVES any existing
    marker file (not a silent no-op: /model with no matching value and the
    Ctrl+B cycle's own reset both rely on "" actually clearing history).
    Any other unrecognised tier is a defensive no-op (callers should already
    validate against TIERS before calling this). Never raises: a write/
    remove failure just means the next boot won't remember, no worse than
    before this feature existed."""
    path = _path_for(workspace)
    if tier == "":
        try:
            os.remove(path)
        except Exception:
            pass
        return
    if tier not in TIERS:
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tier)
    except Exception:
        pass
