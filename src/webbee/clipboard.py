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
    None if nothing usable is installed.

    webbee-code-clipboard-session-sync-v1: on Linux, tries the tool that
    matches the ACTUAL running session (Wayland vs X11) first -- picking
    xclip unconditionally when BOTH tools happen to be installed (common:
    many Wayland distros still ship xclip for X11-app compatibility) wrote
    into the WRONG store -- one clipboard_read.py's own (different) order
    then never checked when reading back. See clipboard_session.py for the
    shared detection both modules now use, so a copy always lands in the
    SAME store the next paste reads from.

    webbee-code-clipboard-xsel-support-v1 (Valentin, live, PopOS: "копирование
    вообще никак не работает"): xsel is a common third X11 clipboard tool
    (some minimal/DE-less Linux setups ship it instead of xclip) that was
    never tried at all -- a box with ONLY xsel installed had zero working
    copy path. Added as the last X11 fallback, same CLIPBOARD buffer
    (`--clipboard`, i.e. xclip's own `-selection clipboard`) so it's the
    SAME store every other tool here targets -- never PRIMARY by accident."""
    if sys.platform == "darwin":
        return ["pbcopy"] if shutil.which("pbcopy") else None
    if sys.platform == "win32":
        if shutil.which("clip.exe"):
            return ["clip.exe"]
        if shutil.which("powershell.exe"):
            return ["powershell.exe", "-NoProfile", "-Command", "[Console]::Input.ReadToEnd() | Set-Clipboard"]
        return None
    from webbee.clipboard_session import is_wayland_session
    if is_wayland_session():
        if shutil.which("wl-copy"):
            return ["wl-copy"]
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"]
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input"]
        return None
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
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
    actually succeeded, never a claim the copy didn't earn.

    webbee-code-clipboard-actionable-failure-v1 (Valentin, live, PopOS:
    "копирование вообще не работает, никак"): a bare "✗ copy failed" left
    the user with zero next step -- on a fresh/minimal Linux desktop (Pop!_OS
    included) NEITHER a clipboard tool NOR OSC 52 terminal support can be
    assumed installed/enabled, so this was the single most likely real dead
    end. On Linux specifically, when the local tool truly isn't installed
    (not just a transient failure), name the exact package to install for
    the session type we already detected -- one apt command, not a guess."""
    if _try_local_copy(text):
        n = len(text)
        return f"✓ copied {n} char{'s' if n != 1 else ''}"
    if _osc52_emit(text):
        return "⇢ sent to terminal clipboard (OSC 52)"
    if sys.platform not in ("darwin", "win32") and _local_copy_cmd() is None:
        from webbee.clipboard_session import is_wayland_session
        pkg = "wl-clipboard" if is_wayland_session() else "xclip"
        return f"✗ copy failed — install it: sudo apt install {pkg}"
    return "✗ copy failed"
