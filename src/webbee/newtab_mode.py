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
