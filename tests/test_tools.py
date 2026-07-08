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


def test_claude_code_arg_synonyms(tmp_path):
    # The brain (a Claude model) often uses Claude-Code arg names:
    # file_path / old_string / new_string / content. All must work.
    ex = _ex(tmp_path)
    assert ex.run("write_file", {"file_path": "a.txt", "content": "hello world"})["ok"]
    assert ex.run("read_file", {"file_path": "a.txt"})["content"] == "hello world"
    assert ex.run("edit_file", {"file_path": "a.txt", "old_string": "world", "new_string": "webbee"})["ok"]
    assert ex.run("read_file", {"path": "a.txt"})["content"] == "hello webbee"


def test_missing_path_is_graceful_not_keyerror(tmp_path):
    # A missing path must NOT surface as a cryptic KeyError: 'path'.
    ex = _ex(tmp_path)
    r = ex.run("write_file", {"content": "x"})
    assert not r["ok"] and "path" in r["content"].lower() and "KeyError" not in r["content"]
    r2 = ex.run("edit_file", {"path": "nope.txt"})   # no old/new
    assert not r2["ok"]


def test_bash_timeout_capped_at_3600(tmp_path, monkeypatch):
    import webbee.tools as T
    captured = {}

    def _fake_run(cmd, **kw):
        captured.update(kw)
        class _P:  # minimal CompletedProcess stand-in
            returncode, stdout, stderr = 0, "ok", ""
        return _P()

    monkeypatch.setattr(T.subprocess, "run", _fake_run)
    ex = T.LocalToolExecutor(str(tmp_path))
    ex.run("bash", {"command": "true", "timeout": 99999})
    assert captured["timeout"] == 3600
    ex.run("bash", {"command": "true"})
    assert captured["timeout"] == 120


def test_cpc_shim_degrades_without_indexer(tmp_path):
    from webbee.tools import LocalToolExecutor
    ex = LocalToolExecutor(str(tmp_path))            # indexer=None
    out = ex.run("graph_slice", {"symbols": ["x"]})
    assert out["ok"] is False and "intel not available" in out["content"]
