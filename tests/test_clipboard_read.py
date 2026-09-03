import subprocess
import sys

import webbee.clipboard_read as cr


class _CP:
    def __init__(self, rc=0, out=b""):
        self.returncode = rc
        self.stdout = out


def test_run_captures_output_tty_safe(monkeypatch):
    # The 0.3.32 lesson: clipboard subprocesses must NOT inherit the dock tty.
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw)
        return _CP(0, b"x")

    monkeypatch.setattr(subprocess, "run", fake)
    cr._run(["echo"])
    assert seen.get("capture_output") is True
    assert seen.get("timeout") == 2


def test_run_never_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    assert cr._run(["x"]) is None


def test_mac_image_via_pngpaste(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cr.shutil, "which", lambda n: "/p" if n == "pngpaste" else None)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: _CP(0, b"\x89PNGdata"))
    assert cr.read_clipboard_image() == b"\x89PNGdata"


def test_image_none_when_no_tool(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cr.shutil, "which", lambda n: None)
    assert cr.read_clipboard_image() is None


def test_linux_image_requires_png_in_targets(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cr.shutil, "which", lambda n: "/x" if n == "xclip" else None)

    def fake(cmd, **k):
        if "TARGETS" in cmd:
            return _CP(0, b"TARGETS\nimage/png\nUTF8_STRING")
        return _CP(0, b"PNGDATA")

    monkeypatch.setattr(subprocess, "run", fake)
    assert cr.read_clipboard_image() == b"PNGDATA"


def test_linux_image_none_when_no_png_target(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cr.shutil, "which", lambda n: "/x" if n == "xclip" else None)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: _CP(0, b"TARGETS\nUTF8_STRING"))
    assert cr.read_clipboard_image() is None


def test_read_clipboard_text(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cr.shutil, "which", lambda n: "/p" if n == "pbpaste" else None)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: _CP(0, b"hello text"))
    assert cr.read_clipboard_text() == "hello text"


def test_read_clipboard_prefers_image_then_text_then_none(monkeypatch):
    monkeypatch.setattr(cr, "read_clipboard_image", lambda: b"IMG")
    monkeypatch.setattr(cr, "read_clipboard_text", lambda: "txt")
    item = cr.read_clipboard("20260721")
    assert item.kind == "image" and item.data == b"IMG"
    assert item.name == "pasted-20260721.png" and item.mime == "image/png"

    monkeypatch.setattr(cr, "read_clipboard_image", lambda: None)
    item = cr.read_clipboard("x")
    assert item.kind == "text" and item.data == "txt"

    monkeypatch.setattr(cr, "read_clipboard_text", lambda: None)
    assert cr.read_clipboard("x") is None


# ── webbee-code-clipboard-session-sync-v1 ─────────────────────────────────

def test_text_cmd_prefers_wlpaste_on_a_real_wayland_session(monkeypatch):
    monkeypatch.setattr(cr.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setattr(cr.shutil, "which",
                        lambda name: "/usr/bin/xclip" if name == "xclip" else
                                    ("/usr/bin/wl-paste" if name == "wl-paste" else None))
    assert cr._text_cmd() == ["wl-paste", "--no-newline"]


def test_text_cmd_still_prefers_xclip_on_a_plain_x11_session(monkeypatch):
    monkeypatch.setattr(cr.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(cr.shutil, "which",
                        lambda name: "/usr/bin/xclip" if name == "xclip" else
                                    ("/usr/bin/wl-paste" if name == "wl-paste" else None))
    assert cr._text_cmd() == ["xclip", "-selection", "clipboard", "-o"]


def test_text_cmd_write_read_agree_on_wayland_with_both_tools(monkeypatch):
    # THE regression this whole fix is about: copy_to_clipboard's tool choice
    # and _text_cmd's tool choice must be the SAME tool on the SAME session.
    import webbee.clipboard as cw
    monkeypatch.setattr(cw.sys, "platform", "linux")
    monkeypatch.setattr(cr.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    both = lambda name: "/usr/bin/xclip" if name == "xclip" else ("/usr/bin/wl-copy" if name == "wl-copy" else ("/usr/bin/wl-paste" if name == "wl-paste" else None))
    monkeypatch.setattr(cw.shutil, "which", both)
    monkeypatch.setattr(cr.shutil, "which", both)
    assert cw._local_copy_cmd()[0] == "wl-copy"
    assert cr._text_cmd()[0] == "wl-paste"   # the SAME family as the writer chose


def test_text_cmd_falls_back_to_xsel_when_nothing_else_installed(monkeypatch):
    """webbee-code-clipboard-xsel-support-v1 (Valentin, live 2026-07-31:
    "все альтернативы должна поддерживаться с правильным буфером"): xsel is
    the last X11 fallback on both read and write, targeting the SAME
    --clipboard buffer copy_to_clipboard's own xsel path writes to."""
    monkeypatch.setattr(cr.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(cr.shutil, "which", lambda name: "/usr/bin/xsel" if name == "xsel" else None)
    assert cr._text_cmd() == ["xsel", "--clipboard", "--output"]


def test_text_cmd_write_read_agree_on_xsel_only_box(monkeypatch):
    import webbee.clipboard as cw
    monkeypatch.setattr(cw.sys, "platform", "linux")
    monkeypatch.setattr(cr.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    only_xsel = lambda name: "/usr/bin/xsel" if name == "xsel" else None
    monkeypatch.setattr(cw.shutil, "which", only_xsel)
    monkeypatch.setattr(cr.shutil, "which", only_xsel)
    assert cw._local_copy_cmd() == ["xsel", "--clipboard", "--input"]
    assert cr._text_cmd() == ["xsel", "--clipboard", "--output"]


def test_mac_image_falls_back_from_png_to_native_tiff_and_sips(monkeypatch, tmp_path):
    """macOS screenshots may expose public.tiff, not PNGf; native sips converts it."""
    monkeypatch.setattr(cr.sys, "platform", "darwin")
    monkeypatch.setattr(cr.shutil, "which", lambda name: "/usr/bin/" + name
                        if name in {"osascript", "sips"} else None)

    calls = []

    def fake_tempfile(argv_for_path):
        calls.append(argv_for_path("/tmp/item"))
        return None if "PNGf" in calls[-1][-1] else b"TIFF"

    monkeypatch.setattr(cr, "_read_via_tempfile", fake_tempfile)
    monkeypatch.setattr(cr.tempfile, "mkstemp", lambda suffix: (99, str(tmp_path / ("in" + suffix))))
    monkeypatch.setattr(cr.os, "close", lambda fd: None)
    monkeypatch.setattr(cr.os.path, "getsize", lambda path: 1)
    monkeypatch.setattr(cr, "_run", lambda cmd: _CP(0, b""))
    monkeypatch.setattr("builtins.open", lambda path, mode: _Open(b"PNG") if "rb" in mode else _Open())

    assert cr._mac_image() == b"PNG"
    assert "PNGf" in calls[0][-1]
    assert "TIFF" in calls[1][-1]


class _Open:
    def __init__(self, read=b""):
        self._read = read
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self._read
    def write(self, data):
        return len(data)
