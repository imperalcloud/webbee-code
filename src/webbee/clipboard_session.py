"""Shared Linux desktop-session detection for clipboard.py (write) and
clipboard_read.py (read) -- ONE source of truth for which tool to prefer,
so a copy and the paste that follows it always target the SAME OS
clipboard store.

Real bug this fixes (Valentin, live 2026-07-31): clipboard.py preferred
xclip over wl-copy, while clipboard_read.py preferred wl-paste over xclip
-- on any Linux box with BOTH tools installed (common: many Wayland
distros still ship xclip for X11-app compatibility), a copy went through
xclip into the X11 CLIPBOARD selection while the very next paste read
wl-paste's Wayland clipboard first -- two entirely separate buffers. That
is exactly "то копирует, то нет" and "между вкладками не вставляется":
every Ctrl+V after a copy was reading the wrong store, tab or no tab.

`XDG_SESSION_TYPE=wayland` (set by every major display manager under a
Wayland session) and/or `WAYLAND_DISPLAY` being non-empty are the two
standard, reliable signals; the absence of both defaults to the X11
assumption `xclip` already had, so this changes nothing on a genuine X11
session or on machines with only one tool installed.
"""
from __future__ import annotations

import os


def is_wayland_session() -> bool:
    """True when the CURRENT session is Wayland, PURE read of env vars
    (never raises, never shells out)."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
