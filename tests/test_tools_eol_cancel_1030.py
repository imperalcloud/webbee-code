"""A file keeps its own line endings through every edit and is never left half-written; a stop ends the commands
running now and every process they started (review 2026-10-30, H2 and H3)."""
import os
import stat
import threading
import time

import webbee.tools as T


def test_a_crlf_file_stays_crlf_through_an_edit_and_a_write(tmp_path):
    p = tmp_path / "win.txt"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    ex = T.LocalToolExecutor(str(tmp_path))
    r = ex.run("edit_file", {"path": "win.txt", "old": "two\nthree", "new": "TWO\nthree"})
    assert r["ok"] and p.read_bytes() == b"one\r\nTWO\r\nthree\r\n"
    assert "TWO" in r["diff"] and r["diff"].count("\n-") <= 2          # the diff is the one line, not the file
    r = ex.run("multi_edit", {"edits": [{"path": "win.txt", "old": "one", "new": "ONE"}]})
    assert r["ok"] and p.read_bytes() == b"ONE\r\nTWO\r\nthree\r\n"
    ex.run("write_file", {"path": "win.txt", "content": "a\nb\n"})
    assert p.read_bytes() == b"a\r\nb\r\n"


def test_an_lf_file_stays_lf_and_a_new_file_is_written_as_given(tmp_path):
    p = tmp_path / "unix.py"
    p.write_bytes(b"x = 1\ny = 2\n")
    ex = T.LocalToolExecutor(str(tmp_path))
    assert ex.run("edit_file", {"path": "unix.py", "old": "y = 2", "new": "y = 3"})["ok"]
    assert p.read_bytes() == b"x = 1\ny = 3\n"
    ex.run("write_file", {"path": "new.txt", "content": "a\nb\n"})
    assert (tmp_path / "new.txt").read_bytes() == b"a\nb\n"


def test_a_write_keeps_the_mode_and_leaves_no_temporary_file(tmp_path):
    p = tmp_path / "run.sh"
    p.write_text("echo hi\n")
    os.chmod(p, 0o755)
    ex = T.LocalToolExecutor(str(tmp_path))
    assert ex.run("edit_file", {"path": "run.sh", "old": "hi", "new": "there"})["ok"]
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o755
    assert sorted(os.listdir(tmp_path)) == ["run.sh"]


def test_a_failed_write_leaves_the_file_whole(tmp_path, monkeypatch):
    p = tmp_path / "a.py"
    p.write_text("keep = 1\n")

    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(T.os, "replace", broken_replace)
    ex = T.LocalToolExecutor(str(tmp_path))
    r = ex.run("edit_file", {"path": "a.py", "old": "keep = 1", "new": "lost = 2"})
    assert r["ok"] is False and p.read_text() == "keep = 1\n"
    assert sorted(os.listdir(tmp_path)) == ["a.py"]


def test_a_stop_ends_the_running_command_and_its_children(tmp_path):
    if os.name == "nt":
        return
    pidfile = tmp_path / "child.pid"
    ex = T.LocalToolExecutor(str(tmp_path))
    out = {}
    th = threading.Thread(target=lambda: out.update(ex.run(
        "bash", {"command": f"sleep 60 & echo $! > {pidfile}; wait", "timeout": 120})))
    th.start()
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    t0 = time.time()
    assert ex.cancel_running() == 1
    th.join(10)
    assert not th.is_alive() and time.time() - t0 < 6 and out.get("ok") is False
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
        alive = True
    except OSError:
        alive = False
    assert not alive and ex.cancel_running() == 0


def test_a_checkpoint_says_what_it_holds_and_what_it_does_not(tmp_path):
    class Shadow:
        def checkpoint(self, label):
            return {"id": "abc", "n": 3, "changed": True, "nested": ["wpanel/core/gateway"],
                    "uncovered": [("data/big.bin", "over 20 MB")]}
    ex = T.LocalToolExecutor(str(tmp_path), shadow=Shadow())
    r = ex.run("checkpoint", {"label": "x"})
    assert "wpanel/core/gateway" in r["content"] and "NOT in it: data/big.bin (over 20 MB)" in r["content"]
