"""Best-effort URL opener (W5 Home: Top-up credits, Read security docs).
There is no existing opener in the client -- this is the minimal one. On a
LOCAL machine `webbrowser.open` launches the default browser; over SSH (the
common Webbee Code case) it no-ops or fails, so the caller ALSO surfaces the
URL as copyable text. Returns the URL unchanged so the caller can show it;
never raises."""
from __future__ import annotations


def open_url(url: str) -> str:
    try:
        import webbrowser
        webbrowser.open(url, new=2)
    except Exception:
        pass
    return url
