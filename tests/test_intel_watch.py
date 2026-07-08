import asyncio
import os
import threading

from webbee.intel import watch


def test_on_change_runs_off_the_event_loop(monkeypatch, tmp_path):
    # F3: watch_workspace must run on_change via asyncio.to_thread, not
    # inline on the asyncio loop -- on a big repo, apply_changes does a full
    # parse + graph rebuild, which would otherwise freeze the dock on every
    # save. Verified by checking on_change actually executes on a different
    # thread than the one driving the event loop.
    root = str(tmp_path)
    calls = []

    def record_on_change(rels):
        calls.append((threading.current_thread(), rels))

    async def fake_awatch(_root):
        yield {("added", os.path.join(root, "a.py"))}

    monkeypatch.setattr("watchfiles.awatch", fake_awatch)
    main_thread = threading.current_thread()

    asyncio.run(watch.watch_workspace(root, record_on_change))

    assert len(calls) == 1
    thread, rels = calls[0]
    assert thread is not main_thread
    assert rels == {"a.py"}


def test_git_and_node_modules_paths_are_filtered(monkeypatch, tmp_path):
    root = str(tmp_path)
    calls = []

    def record_on_change(rels):
        calls.append(rels)

    async def fake_awatch(_root):
        yield {
            ("modified", os.path.join(root, ".git", "index")),
            ("modified", os.path.join(root, "node_modules", "x.js")),
            ("modified", os.path.join(root, "a.py")),
        }

    monkeypatch.setattr("watchfiles.awatch", fake_awatch)
    asyncio.run(watch.watch_workspace(root, record_on_change))

    assert calls == [{"a.py"}]


def test_missing_watchfiles_extra_returns_immediately(monkeypatch, tmp_path):
    import builtins
    real_import = builtins.__import__

    def _blocked_import(name, *a, **kw):
        if name == "watchfiles":
            raise ImportError("no watchfiles")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    # Must return (not hang/raise) when the optional extra isn't installed.
    asyncio.run(watch.watch_workspace(str(tmp_path), lambda rels: None))
