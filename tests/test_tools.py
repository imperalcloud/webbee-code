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
    # run() must DENY an out-of-workspace path but return a graceful result —
    # NOT raise. A re-raise escaped run(), the reverse-channel handler never
    # posted a result, and the kernel hung waiting (frozen dock). See
    # test_freeze_fix.py.
    ex = _ex(tmp_path)
    r = ex.run("read_file", {"path": "../../etc/passwd"})
    assert r["ok"] is False
    assert "outside the workspace" in r["content"].lower()

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


def test_cpc_graph_slice_coerces_stringified_symbols(tmp_path):
    # An "any LLM" surface may emit symbols as a bare string or a stringified
    # JSON array instead of a real list. Without coercion, query.graph_slice
    # iterates the string char-by-char and silently returns total:0 -- a
    # false negative the brain reads as "no callers".
    pytest.importorskip("tree_sitter")
    from webbee.intel.service import IntelService
    (tmp_path / "a.py").write_text("def alpha():\n    return beta()\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / "c"))
    svc.build()
    ex = LocalToolExecutor(str(tmp_path), indexer=svc)

    r1 = ex.run("graph_slice", {"symbols": "beta"})
    assert r1["ok"] and any(i["title"] == "beta" for i in r1["data"]["items"])

    r2 = ex.run("graph_slice", {"symbols": '["beta"]'})
    assert r2["ok"] and any(i["title"] == "beta" for i in r2["data"]["items"])

    r3 = ex.run("impact_of_change", {"symbols": "beta"})
    assert r3["ok"] and any(i["id"].startswith("a.py") for i in r3["data"]["items"])


def test_edit_file_rejects_ambiguous_old(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "x = 1\nx = 1\n"})
    r = ex.run("edit_file", {"path": "a.txt", "old": "x = 1", "new": "x = 2"})
    assert not r["ok"] and "2 times" in r["content"]
    # untouched on failure
    assert ex.run("read_file", {"path": "a.txt"})["content"] == "x = 1\nx = 1\n"


def test_edit_file_replace_all(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "x = 1\nx = 1\n"})
    r = ex.run("edit_file", {"path": "a.txt", "old": "x = 1", "new": "x = 2",
                             "replace_all": True})
    assert r["ok"]
    assert ex.run("read_file", {"path": "a.txt"})["content"] == "x = 2\nx = 2\n"


def test_multi_edit_applies_across_files(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "def old_name():\n    pass\n"})
    ex.run("write_file", {"path": "b.py", "content": "from a import old_name\n"})
    r = ex.run("multi_edit", {"edits": [
        {"path": "a.py", "old": "def old_name", "new": "def new_name"},
        {"path": "b.py", "old": "import old_name", "new": "import new_name"},
    ]})
    assert r["ok"] and "2 edits" in r["content"]
    assert "new_name" in ex.run("read_file", {"path": "a.py"})["content"]
    assert "new_name" in ex.run("read_file", {"path": "b.py"})["content"]


def test_multi_edit_is_all_or_nothing(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "alpha\n"})
    ex.run("write_file", {"path": "b.py", "content": "beta\n"})
    r = ex.run("multi_edit", {"edits": [
        {"path": "a.py", "old": "alpha", "new": "ALPHA"},
        {"path": "b.py", "old": "MISSING", "new": "x"},
    ]})
    assert not r["ok"] and "applied NOTHING" in r["content"] and "b.py" in r["content"]
    assert ex.run("read_file", {"path": "a.py"})["content"] == "alpha\n"   # untouched


def test_multi_edit_same_file_edits_compose(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "one\ntwo\n"})
    r = ex.run("multi_edit", {"edits": [
        {"path": "a.py", "old": "one", "new": "ONE"},
        {"path": "a.py", "old": "two", "new": "TWO"},
    ]})
    assert r["ok"]
    assert ex.run("read_file", {"path": "a.py"})["content"] == "ONE\nTWO\n"


def test_multi_edit_outside_workspace_rejected(tmp_path):
    ex = _ex(tmp_path)
    r = ex.run("multi_edit", {"edits": [
        {"path": "../evil.txt", "old": "a", "new": "b"}]})
    assert not r["ok"] and "applied NOTHING" in r["content"]


def test_edit_file_replace_all_string_false_is_false(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "x = 1\nx = 1\n"})
    r = ex.run("edit_file", {"path": "a.txt", "old": "x = 1", "new": "x = 2",
                             "replace_all": "false"})
    assert not r["ok"] and "2 times" in r["content"]        # stringly false != True


def test_multi_edit_unwritable_target_fails_validation(tmp_path):
    import os as _os
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "alpha\n"})
    ex.run("write_file", {"path": "ro.py", "content": "beta\n"})
    _os.chmod(str(tmp_path / "ro.py"), 0o444)
    try:
        r = ex.run("multi_edit", {"edits": [
            {"path": "a.py", "old": "alpha", "new": "ALPHA"},
            {"path": "ro.py", "old": "beta", "new": "BETA"},
        ]})
        assert not r["ok"] and "applied NOTHING" in r["content"]
        assert "not writable" in r["content"]
        assert ex.run("read_file", {"path": "a.py"})["content"] == "alpha\n"   # untouched
    finally:
        _os.chmod(str(tmp_path / "ro.py"), 0o644)


class _FakeShadow:
    def __init__(self):
        self.labels = []
        self.rolled = []
        self.auto_ok = True
    def checkpoint(self, label=""):
        self.labels.append(label)
        return {"id": "abc1234", "n": len(self.labels), "label": label, "changed": True}
    def note_auto_result(self, ok):
        pass
    def diff(self, since=""):
        return f"DIFF since={since or 'HEAD'}"
    def rollback(self, to):
        self.rolled.append(to)
        return {"ok": True, "content": f"restored to {to}"}


def _ex_shadow(tmp_path):
    from webbee.tools import LocalToolExecutor
    sh = _FakeShadow()
    return LocalToolExecutor(str(tmp_path), shadow=sh), sh


def test_write_tools_auto_checkpoint_first(tmp_path):
    ex, sh = _ex_shadow(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "x"})
    ex.run("edit_file", {"path": "a.txt", "old": "x", "new": "y"})
    ex.run("bash", {"command": "true"})
    assert sh.labels == ["pre:write_file", "pre:edit_file", "pre:bash"]


def test_read_tools_do_not_checkpoint(tmp_path):
    ex, sh = _ex_shadow(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "x"})
    ex.run("read_file", {"path": "a.txt"})
    ex.run("grep", {"pattern": "x"})
    assert sh.labels == ["pre:write_file"]


def test_shadow_failure_never_blocks_the_write(tmp_path):
    from webbee.tools import LocalToolExecutor

    class _Boom:
        def checkpoint(self, label=""):
            raise RuntimeError("shadow down")

    ex = LocalToolExecutor(str(tmp_path), shadow=_Boom())
    r = ex.run("write_file", {"path": "a.txt", "content": "x"})
    assert r["ok"]                                          # the work still happened


def test_checkpoint_diff_rollback_shims(tmp_path):
    ex, sh = _ex_shadow(tmp_path)
    r = ex.run("checkpoint", {"label": "before refactor"})
    assert r["ok"] and "cp-" in r["content"]
    r = ex.run("diff", {"since": "cp-1"})
    assert r["ok"] and r["content"] == "DIFF since=cp-1"
    r = ex.run("rollback", {"checkpoint": "cp-1"})
    assert r["ok"] and sh.rolled == ["cp-1"]
    r = ex.run("rollback", {})
    assert not r["ok"] and "requires" in r["content"]


def test_reversibility_tools_honest_without_shadow(tmp_path):
    ex = _ex(tmp_path)                                      # no shadow wired
    for tool, args in (("checkpoint", {}), ("diff", {}), ("rollback", {"checkpoint": "1"})):
        r = ex.run(tool, args)
        assert not r["ok"] and "unavailable" in r["content"]


def test_auto_checkpoint_latches_off_after_consecutive_failures(tmp_path):
    # Adapted from the P4 latch test (final-review F8): a SINGLE failed AUTO
    # snapshot must no longer latch auto-checkpointing off; it now takes
    # _AUTO_FAIL_LATCH CONSECUTIVE failures, driven through the real
    # ShadowGit.note_auto_result via the executor.
    from webbee.tools import LocalToolExecutor
    from webbee.checkpoints import ShadowGit

    sg = ShadowGit(str(tmp_path), "rk_f8_latch", cache_dir=str(tmp_path / "c3"))
    assert sg.ensure()
    calls = {"n": 0}
    def _always_fails(label=""):
        calls["n"] += 1
        return None                          # every AUTO snapshot fails
    sg.checkpoint = _always_fails
    ex = LocalToolExecutor(str(tmp_path), shadow=sg)

    for i in range(sg._AUTO_FAIL_LATCH - 1):
        ex.run("write_file", {"path": f"f{i}.txt", "content": str(i)})
        assert sg.auto_ok is True             # still enabled before the Nth failure

    ex.run("write_file", {"path": "final.txt", "content": "x"})
    assert calls["n"] == sg._AUTO_FAIL_LATCH  # latched only after N consecutive failures
    assert sg.auto_ok is False


def test_executor_single_transient_does_not_latch(tmp_path):
    from webbee.tools import LocalToolExecutor
    from webbee.checkpoints import ShadowGit
    sg = ShadowGit(str(tmp_path), "rk_f8", cache_dir=str(tmp_path / "c"))
    assert sg.ensure()
    calls = {"n": 0}
    def _flaky(label=""):
        calls["n"] += 1
        return None if calls["n"] == 1 else {"id": "x", "n": calls["n"], "label": label, "changed": True}
    sg.checkpoint = _flaky
    ex = LocalToolExecutor(str(tmp_path), shadow=sg)
    ex.run("write_file", {"path": "a.txt", "content": "1"})   # transient miss
    ex.run("write_file", {"path": "b.txt", "content": "2"})   # success -> streak reset
    assert sg.auto_ok is True                                  # still enabled
