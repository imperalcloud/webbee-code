import os
import subprocess
import sys

import webbee.urlopen as urlopen


def test_open_url_launches_detached_and_silenced(monkeypatch):
    # Force the darwin path so a launch is always attempted regardless of the
    # test host, then assert the child's std streams are ALL silenced (so a
    # chatty browser can't corrupt the TUI) and it's detached.
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = {}

    def fake_popen(argv, **kw):
        calls["argv"] = argv
        calls["kw"] = kw
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    out = urlopen.open_url("https://panel.imperal.io/ext/billing")
    assert out == "https://panel.imperal.io/ext/billing"
    assert "https://panel.imperal.io/ext/billing" in calls["argv"]
    assert calls["kw"]["stdin"] == subprocess.DEVNULL
    assert calls["kw"]["stdout"] == subprocess.DEVNULL
    assert calls["kw"]["stderr"] == subprocess.DEVNULL
    assert calls["kw"].get("start_new_session") is True


def test_open_url_skips_launch_on_headless_linux(monkeypatch):
    # No DISPLAY/WAYLAND on Linux == SSH/headless: don't spawn anything, just
    # return the URL for the caller to show as copyable text.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    called = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: called.append(1))
    assert urlopen.open_url("https://x") == "https://x"
    assert called == []


def test_open_url_never_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*a, **k):
        raise RuntimeError("no opener")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert urlopen.open_url("https://x") == "https://x"
