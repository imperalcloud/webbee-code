"""Best-effort URL opener (W5 Home: Top-up credits, Read security docs).

The browser is launched FULLY DETACHED and SILENCED — its own session, with
stdin/stdout/stderr all routed to /dev/null — so a chatty browser can NEVER
spew into and corrupt the full-screen terminal. (A real Ubuntu user clicked
"Top up credits" and Chrome's `MESA-LOADER` / GCM / lens-overlay JS logs
poured straight into the TUI, scrambling it — that inherited-fd path is what
this guards against.) On a pure SSH / headless Linux session there is no
browser to open, so we don't even try — the caller ALWAYS shows the URL as
copyable text, which is the reliable path there. Returns the URL unchanged so
the caller can display it; never raises, never blocks."""
from __future__ import annotations

import os
import subprocess
import sys


def open_url(url: str) -> str:
    try:
        if sys.platform == "darwin":
            argv = ["open", url]
        elif os.name == "nt":
            argv = ["cmd", "/c", "start", "", url]
        else:
            # Linux / BSD: only attempt a launch when a graphical session is
            # actually present. No DISPLAY/WAYLAND == SSH/headless == nothing
            # to open (and nothing that could pollute the terminal).
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return url
            argv = ["xdg-open", url]
        devnull = subprocess.DEVNULL
        subprocess.Popen(argv, stdin=devnull, stdout=devnull, stderr=devnull,
                         start_new_session=True)
    except Exception:
        pass
    return url
