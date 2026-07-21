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
