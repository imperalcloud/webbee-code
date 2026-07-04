"""Copy-on-select's actual clipboard write. webbee runs LOCALLY on the user's
machine, so the real OS clipboard tool is tried FIRST (`pbcopy` on macOS,
`xclip`/`wl-copy` on Linux); OSC 52 is only a FALLBACK, useful if the CLI is
ever run over SSH. Terminal.app does not support OSC 52 at all and iTerm2
needs a permission toggle — the old code emitted OSC 52 unconditionally and
flashed "copied" regardless, so the clipboard silently stayed empty. The
flash label returned here is honest: it reflects what actually happened."""
from __future__ import annotations

import shutil
import subprocess
import sys


def _local_copy_cmd() -> list[str] | None:
    """The first available local clipboard command for this platform, or
    None if nothing usable is installed."""
    if sys.platform == "darwin":
        return ["pbcopy"] if shutil.which("pbcopy") else None
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    return None


def _try_local_copy(text: str) -> bool:
    """Feed `text` to the local clipboard tool via stdin. True only on a
    clean (returncode 0) run — never raises."""
    cmd = _local_copy_cmd()
    if cmd is None:
        return False
    try:
        proc = subprocess.run(cmd, input=text.encode("utf-8", "replace"), timeout=2)
        return proc.returncode == 0
    except Exception:
        return False


def _osc52_emit(text: str) -> bool:
    """Fallback clipboard write via the OSC 52 escape sequence — only useful
    when the terminal actually honors it (most don't, by default)."""
    try:
        import base64

        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
        if app is None:
            return False
        b64 = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
        app.output.write_raw("\x1b]52;c;" + b64 + "\x07")
        app.output.flush()
        return True
    except Exception:
        return False


def copy_to_clipboard(text: str) -> str:
    """Copy `text` to the clipboard, local tool first, OSC 52 as a fallback.
    Returns the toolbar flash label — HONEST about which path (if any)
    actually succeeded, never a claim the copy didn't earn."""
    if _try_local_copy(text):
        n = len(text)
        return f"✓ copied {n} char{'s' if n != 1 else ''}"
    if _osc52_emit(text):
        return "⇢ sent to terminal clipboard (OSC 52)"
    return "✗ copy failed"
