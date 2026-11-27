"""The person's own display choices, kept across restarts (one small file each under ``~/.cache/webbee``).

``kernel_notes`` -- whether the kernel's line of facts is printed under Webbee's answers (the owner, 2026-11-27:
"in the terminal I would make kernel facts a setting, off by default, that can be switched on"). It is a choice of
the PERSON, not of a repository: one file for every workspace. It travels with each turn (``coding_context
["kernel_notes"]``) and only changes what is printed -- the kernel checks an answer and hands a wrong one back
either way.

Fail-soft, like ``tier_store``: an unreadable folder reads as the default, a failed write is dropped."""
from __future__ import annotations

import os

_CACHE_DIR = os.path.expanduser("~/.cache/webbee")   # test seam: monkeypatch this name
_KERNEL_NOTES = "kernel-notes"


def kernel_notes() -> bool:
    """True when the person switched the kernel's line under an answer on. Off when never set or unreadable."""
    try:
        with open(os.path.join(_CACHE_DIR, _KERNEL_NOTES), "r", encoding="utf-8") as f:
            return f.read().strip() == "on"
    except Exception:
        return False


def set_kernel_notes(on: bool) -> bool:
    """Remember the choice; True when it was written. Off removes the file -- off is the state of no file."""
    path = os.path.join(_CACHE_DIR, _KERNEL_NOTES)
    try:
        if not on:
            if os.path.exists(path):
                os.remove(path)
            return True
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("on")
        return True
    except Exception:
        return False
