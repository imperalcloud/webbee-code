import pytest
from webbee.tools import LocalToolExecutor, OutsideWorkspaceError

def _ex(tmp_path):
    return LocalToolExecutor(str(tmp_path))

def test_write_then_read(tmp_path):
    ex = _ex(tmp_path)
    assert ex.run("write_file", {"path": "a.txt", "content": "hi"})["ok"]
    r = ex.run("read_file", {"path": "a.txt"})
    assert r["ok"] and r["content"] == "hi"

def test_edit_file(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "hello world"})
    assert ex.run("edit_file", {"path": "a.txt", "old": "world", "new": "webbee"})["ok"]
    assert ex.run("read_file", {"path": "a.txt"})["content"] == "hello webbee"

def test_bash_runs_in_workspace(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "x.txt", "content": "1"})
    r = ex.run("bash", {"command": "ls"})
    assert r["ok"] and "x.txt" in r["content"]

def test_grep(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "def foo():\n    pass\n"})
    r = ex.run("grep", {"pattern": "def foo"})
    assert r["ok"] and "a.py" in r["content"]

def test_outside_workspace_denied(tmp_path):
    ex = _ex(tmp_path)
    with pytest.raises(OutsideWorkspaceError):
        ex.run("read_file", {"path": "../../etc/passwd"})

def test_unknown_tool(tmp_path):
    r = _ex(tmp_path).run("nope", {})
    assert not r["ok"]
