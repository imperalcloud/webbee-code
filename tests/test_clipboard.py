"""Copy-on-select must use the REAL local clipboard (webbee runs locally on
the user's machine) before ever touching OSC 52, and the toolbar flash must
be HONEST about what actually happened. Never touches the real clipboard —
subprocess.run and the OSC 52 emitter are monkeypatched throughout."""
from webbee import clipboard as C


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


def test_darwin_uses_pbcopy(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "darwin")
    monkeypatch.setattr(C.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    seen = {}

    def fake_run(cmd, input=None, timeout=None):
        seen["cmd"] = cmd
        seen["input"] = input
        return _Proc(0)
    monkeypatch.setattr(C.subprocess, "run", fake_run)

    label = C.copy_to_clipboard("hello")
    assert seen["cmd"] == ["pbcopy"]
    assert seen["input"] == b"hello"
    assert label == "✓ copied 5 chars"


def test_singular_char_wording(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "darwin")
    monkeypatch.setattr(C.shutil, "which", lambda name: "/usr/bin/pbcopy")
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **kw: _Proc(0))
    assert C.copy_to_clipboard("x") == "✓ copied 1 char"


def test_linux_prefers_xclip_over_wlcopy(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "linux")
    monkeypatch.setattr(C.shutil, "which",
                        lambda name: "/usr/bin/xclip" if name == "xclip" else
                                    ("/usr/bin/wl-copy" if name == "wl-copy" else None))
    seen = {}

    def fake_run(cmd, input=None, timeout=None):
        seen["cmd"] = cmd
        return _Proc(0)
    monkeypatch.setattr(C.subprocess, "run", fake_run)

    C.copy_to_clipboard("x")
    assert seen["cmd"] == ["xclip", "-selection", "clipboard"]


def test_linux_falls_back_to_wlcopy_when_no_xclip(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "linux")
    monkeypatch.setattr(C.shutil, "which", lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None)
    seen = {}

    def fake_run(cmd, input=None, timeout=None):
        seen["cmd"] = cmd
        return _Proc(0)
    monkeypatch.setattr(C.subprocess, "run", fake_run)

    C.copy_to_clipboard("x")
    assert seen["cmd"] == ["wl-copy"]


def test_no_local_tool_falls_back_to_osc52(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "linux")
    monkeypatch.setattr(C.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(C, "_osc52_emit", lambda text: calls.append(text) or True)

    label = C.copy_to_clipboard("hi")
    assert calls == ["hi"]
    assert label == "⇢ sent to terminal clipboard (OSC 52)"


def test_local_tool_nonzero_exit_falls_back_to_osc52(monkeypatch):
    # macOS Terminal.app has no OSC 52 support and no local failure either —
    # this covers the case where the tool ran but reported failure.
    monkeypatch.setattr(C.sys, "platform", "darwin")
    monkeypatch.setattr(C.shutil, "which", lambda name: "/usr/bin/pbcopy")
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **kw: _Proc(1))
    monkeypatch.setattr(C, "_osc52_emit", lambda text: True)

    label = C.copy_to_clipboard("hi")
    assert "OSC 52" in label


def test_local_tool_exception_falls_back_to_osc52(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "darwin")
    monkeypatch.setattr(C.shutil, "which", lambda name: "/usr/bin/pbcopy")

    def boom(*a, **kw):
        raise OSError("no pbcopy")
    monkeypatch.setattr(C.subprocess, "run", boom)
    monkeypatch.setattr(C, "_osc52_emit", lambda text: True)

    label = C.copy_to_clipboard("hi")
    assert "OSC 52" in label


def test_nothing_available_reports_failure_honestly(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "darwin")
    monkeypatch.setattr(C.shutil, "which", lambda name: None)
    monkeypatch.setattr(C, "_osc52_emit", lambda text: False)

    assert C.copy_to_clipboard("hi") == "✗ copy failed"


def test_osc52_emit_no_running_app_returns_false():
    # Headless test process — no prompt_toolkit Application is running.
    assert C._osc52_emit("hi") is False
