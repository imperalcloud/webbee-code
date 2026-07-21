"""Read the OS clipboard OUT-OF-BAND for Ctrl+V paste (W3 Wave A).

A terminal's bracketed paste is text-only — a clipboard IMAGE never reaches the
app that way — so an image must be pulled from the OS clipboard via the
platform tool. Same discipline as `clipboard.py` (copy-out): `shutil.which`-
gated, bounded timeout, output CAPTURED so it never inherits the dock's tty
(the 0.3.32 lesson: a chatty child scrambles the full-screen renderer), and it
NEVER raises. macOS + Linux are the verified paths; Windows is best-effort and
UNVERIFIED (no Windows host to test on) — it fails soft to None."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ClipboardItem:
    kind: str          # "image" | "text"
    data: object       # bytes (PNG) for image, str for text
    name: str = ""     # suggested filename for an image
    mime: str = ""


def _run(cmd):
    """Captured, bounded, never-raises subprocess. `capture_output=True` keeps
    stdout/stderr OFF the dock's tty. Returns CompletedProcess or None."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=2)
    except Exception:
        return None


def _read_via_tempfile(argv_for_path) -> "bytes | None":
    """Run a tool that WRITES a PNG to a temp path (osascript / PowerShell),
    then read+delete it. `argv_for_path(path)` returns the argv. Never raises."""
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        p = _run(argv_for_path(path))
        if p is not None and p.returncode == 0 and os.path.getsize(path) > 0:
            with open(path, "rb") as f:
                return f.read() or None
        return None
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _mac_image() -> "bytes | None":
    if shutil.which("pngpaste"):
        p = _run(["pngpaste", "-"])
        if p is not None and p.returncode == 0 and p.stdout:
            return p.stdout
    if not shutil.which("osascript"):
        return None
    # osascript returns non-zero when the clipboard holds no «class PNGf» image.
    def _argv(path):
        script = ('set p to (POSIX file "%s")\n'
                  'set d to (the clipboard as «class PNGf»)\n'
                  'set fh to open for access p with write permission\n'
                  'write d to fh\nclose access fh' % path)
        return ["osascript", "-e", script]
    return _read_via_tempfile(_argv)


def _linux_image() -> "bytes | None":
    if shutil.which("wl-paste"):
        types = _run(["wl-paste", "--list-types"])
        if types is not None and types.returncode == 0 and b"image/png" in (types.stdout or b""):
            p = _run(["wl-paste", "--type", "image/png"])
            if p is not None and p.returncode == 0 and p.stdout:
                return p.stdout
    if shutil.which("xclip"):
        tgt = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
        if tgt is not None and tgt.returncode == 0 and b"image/png" in (tgt.stdout or b""):
            p = _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
            if p is not None and p.returncode == 0 and p.stdout:
                return p.stdout
    return None


def _windows_image() -> "bytes | None":
    # UNVERIFIED (no Windows host). Best-effort; fail-soft to None.
    if not shutil.which("powershell"):
        return None

    def _argv(path):
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$i=[System.Windows.Forms.Clipboard]::GetImage();"
              "if($i){$i.Save('%s')}" % path.replace("\\", "\\\\"))
        return ["powershell", "-NoProfile", "-Command", ps]
    return _read_via_tempfile(_argv)


def read_clipboard_image() -> "bytes | None":
    """Raw PNG bytes on the clipboard, or None. Platform-dispatched, captured
    (tty-safe), never raises."""
    try:
        if sys.platform == "darwin":
            return _mac_image()
        if sys.platform == "win32":
            return _windows_image()
        return _linux_image()
    except Exception:
        return None


def _text_cmd() -> "list[str] | None":
    if sys.platform == "darwin":
        return ["pbpaste"] if shutil.which("pbpaste") else None
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if sys.platform == "win32" and shutil.which("powershell"):
        return ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
    return None


def read_clipboard_text() -> "str | None":
    """The clipboard's text, or None. Captured (tty-safe), never raises."""
    cmd = _text_cmd()
    if cmd is None:
        return None
    p = _run(cmd)
    if p is not None and p.returncode == 0 and p.stdout:
        try:
            return p.stdout.decode("utf-8", "replace")
        except Exception:
            return None
    return None


def read_clipboard(ts: str) -> "ClipboardItem | None":
    """One paste's worth of clipboard content: an IMAGE if present (PNG bytes,
    named `pasted-<ts>.png`), else TEXT, else None. `ts` is a caller-supplied
    timestamp string for the image name (the dock has no wall-clock in a pure
    function, so it's passed in)."""
    img = read_clipboard_image()
    if img:
        return ClipboardItem(kind="image", data=img, name=f"pasted-{ts}.png",
                             mime="image/png")
    txt = read_clipboard_text()
    if txt:
        return ClipboardItem(kind="text", data=txt)
    return None
