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


def _run(cmd, timeout=2):
    """Captured, bounded, never-raises subprocess.  keeps
    stdout/stderr OFF the dock's tty, stdin=DEVNULL prevents interactive prompts
    from hanging the process, and non-blocking timeout guarantees quick fail-soft."""
    try:
        import os
        env = os.environ.copy()
        env.setdefault("COMMAND_MODE", "unix2003")
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env)
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
    # webbee-code-clipboard-session-sync-v1: same session-aware order as
    # clipboard.py's write path -- a copy on THIS session's tool must be
    # read back via that SAME tool, not whichever of wl-paste/xclip happens
    # to be probed first regardless of what's actually running.
    from webbee.clipboard_session import is_wayland_session
    tools = ("wl-paste", "xclip") if is_wayland_session() else ("xclip", "wl-paste")
    for tool in tools:
        if tool == "wl-paste" and shutil.which("wl-paste"):
            types = _run(["wl-paste", "--list-types"])
            if types is not None and types.returncode == 0 and b"image/png" in (types.stdout or b""):
                p = _run(["wl-paste", "--type", "image/png"])
                if p is not None and p.returncode == 0 and p.stdout:
                    return p.stdout
        elif tool == "xclip" and shutil.which("xclip"):
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
    if sys.platform == "win32" and shutil.which("powershell"):
        return ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
    # webbee-code-clipboard-session-sync-v1: match clipboard.py's write-side
    # order exactly -- a Wayland session tries wl-paste first, an X11
    # session tries xclip first, so a copy always reads back from the SAME
    # store it was written to (previously this read wl-paste first
    # UNCONDITIONALLY while the writer preferred xclip -- on a box with
    # both installed the copy and the paste hit two different clipboards).
    # webbee-code-clipboard-xsel-support-v1: xsel as the last X11 fallback,
    # same CLIPBOARD buffer (`--clipboard`) the write side now also uses --
    # matters on a box where xsel is the ONLY tool installed (some minimal
    # Linux setups ship it instead of xclip).
    from webbee.clipboard_session import is_wayland_session
    if is_wayland_session():
        if shutil.which("wl-paste"):
            return ["wl-paste", "--no-newline"]
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard", "-o"]
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--output"]
        return None
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"]
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


def read_primary_text() -> "str | None":
    """X11/Wayland PRIMARY selection text (the 'last selected' buffer, distinct
    from CLIPBOARD) -- middle-click paste's own source per the Linux
    select-then-middle-click convention. macOS/Windows have no PRIMARY
    concept, so this is a no-op (None) there -- callers fall back to
    read_clipboard_text(). Captured (tty-safe), never raises."""
    if sys.platform == "darwin" or sys.platform == "win32":
        return None
    # webbee-code-clipboard-session-sync-v1: same session-aware order as
    # the CLIPBOARD-selection paths above, for consistency.
    from webbee.clipboard_session import is_wayland_session
    if is_wayland_session() and shutil.which("wl-paste"):
        p = _run(["wl-paste", "--primary", "--no-newline"])
    elif shutil.which("xclip"):
        p = _run(["xclip", "-selection", "primary", "-o"])
    elif shutil.which("wl-paste"):
        p = _run(["wl-paste", "--primary", "--no-newline"])
    elif shutil.which("xsel"):
        # webbee-code-clipboard-xsel-support-v1: last X11 fallback for
        # PRIMARY too, mirrors read_clipboard_text's own xsel addition.
        p = _run(["xsel", "--primary", "--output"])
    else:
        return None
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
