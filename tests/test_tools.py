import os
import time

import pytest
from webbee.tools import LocalToolExecutor, OutsideWorkspaceError, _relative_time

def _ex(tmp_path):
    return LocalToolExecutor(str(tmp_path))

def _body(r):
    # read_file content = ONE bracketed metadata header line + "\n" + the
    # byte-exact file text.
    return r["content"].split("\n", 1)[1]

def test_write_then_read(tmp_path):
    ex = _ex(tmp_path)
    assert ex.run("write_file", {"path": "a.txt", "content": "hi"})["ok"]
    r = ex.run("read_file", {"path": "a.txt"})
    assert r["ok"] and _body(r) == "hi"

def test_edit_file(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "hello world"})
    assert ex.run("edit_file", {"path": "a.txt", "old": "world", "new": "webbee"})["ok"]
    assert _body(ex.run("read_file", {"path": "a.txt"})) == "hello webbee"

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
    assert _body(ex.run("read_file", {"file_path": "a.txt"})) == "hello world"
    assert ex.run("edit_file", {"file_path": "a.txt", "old_string": "world", "new_string": "webbee"})["ok"]
    assert _body(ex.run("read_file", {"path": "a.txt"})) == "hello webbee"


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
    assert _body(ex.run("read_file", {"path": "a.txt"})) == "x = 1\nx = 1\n"


def test_edit_file_replace_all(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "x = 1\nx = 1\n"})
    r = ex.run("edit_file", {"path": "a.txt", "old": "x = 1", "new": "x = 2",
                             "replace_all": True})
    assert r["ok"]
    assert _body(ex.run("read_file", {"path": "a.txt"})) == "x = 2\nx = 2\n"


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
    assert _body(ex.run("read_file", {"path": "a.py"})) == "alpha\n"   # untouched


def test_multi_edit_same_file_edits_compose(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "one\ntwo\n"})
    r = ex.run("multi_edit", {"edits": [
        {"path": "a.py", "old": "one", "new": "ONE"},
        {"path": "a.py", "old": "two", "new": "TWO"},
    ]})
    assert r["ok"]
    assert _body(ex.run("read_file", {"path": "a.py"})) == "ONE\nTWO\n"


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
        assert _body(ex.run("read_file", {"path": "a.py"})) == "alpha\n"   # untouched
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


# --- read_file metadata header (size + freshness on EVERY read) --------------

def test_read_file_header_metadata(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "one\ntwo\nthree\n"})
    r = ex.run("read_file", {"path": "a.py"})
    assert r["ok"]
    header, body = r["content"].split("\n", 1)
    assert header.startswith("⟦ ") and header.endswith(" ⟧")
    assert "a.py" in header and "3 lines" in header and "modified just now" in header
    assert body == "one\ntwo\nthree\n"          # byte-exact after the header
    assert r["total_lines"] == 3                 # structured twins for any consumer
    assert isinstance(r["modified"], int) and abs(r["modified"] - time.time()) < 60
    assert r["modified_iso"].startswith("20")    # ISO stamp, e.g. 2026-07-17T...


def test_read_file_empty_file_zero_lines(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "empty.txt", "content": ""})
    r = ex.run("read_file", {"path": "empty.txt"})
    assert "0 lines" in r["content"].split("\n", 1)[0]
    assert r["total_lines"] == 0 and _body(r) == ""


def test_read_header_never_breaks_string_edits(tmp_path):
    # edit_file matches `old` against the ON-DISK text (re-read at edit time);
    # the header exists only in the tool RESULT. An old-string copied straight
    # out of the read body must still match byte-exactly.
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "def f():\n    return 1\n"})
    body = _body(ex.run("read_file", {"path": "a.py"}))
    old = body.split("\n")[1]                    # "    return 1" from the read result
    assert ex.run("edit_file", {"path": "a.py", "old": old, "new": "    return 2"})["ok"]
    assert _body(ex.run("read_file", {"path": "a.py"})) == "def f():\n    return 2\n"


def test_relative_time_buckets():
    now = 1_700_000_000.0
    assert _relative_time(now - 5, now) == "just now"
    assert _relative_time(now - 300, now) == "5m ago"
    assert _relative_time(now - 3 * 3600, now) == "3h ago"
    assert _relative_time(now - 2 * 86400, now) == "2d ago"
    assert _relative_time(now - 30 * 86400, now).startswith("20")   # calendar date


def _bump_mtime(path):
    # Simulate an EXTERNAL edit: content change + a strictly newer mtime
    # (deterministic even on filesystems with coarse timestamps).
    st = os.stat(str(path))
    os.utime(str(path), ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_stale_edit_warns_but_never_blocks(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "alpha beta\n"})
    ex.run("read_file", {"path": "a.txt"})
    (tmp_path / "a.txt").write_text("alpha beta gamma\n")   # changed under the agent
    _bump_mtime(tmp_path / "a.txt")
    r = ex.run("edit_file", {"path": "a.txt", "old": "beta", "new": "BETA"})
    assert r["ok"]                                          # informs, NEVER blocks
    assert "changed on disk" in r["content"]
    assert _body(ex.run("read_file", {"path": "a.txt"})) == "alpha BETA gamma\n"


def test_stale_write_file_warns(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "v1\n"})
    ex.run("read_file", {"path": "a.txt"})
    _bump_mtime(tmp_path / "a.txt")                          # external change
    r = ex.run("write_file", {"path": "a.txt", "content": "v2\n"})
    assert r["ok"] and "changed on disk" in r["content"]     # clobber heads-up
    assert _body(ex.run("read_file", {"path": "a.txt"})) == "v2\n"


def test_stale_multi_edit_warns_per_file(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "alpha\n"})
    ex.run("write_file", {"path": "b.py", "content": "beta\n"})
    ex.run("read_file", {"path": "a.py"})
    ex.run("read_file", {"path": "b.py"})
    _bump_mtime(tmp_path / "b.py")                           # only b.py went stale
    r = ex.run("multi_edit", {"edits": [
        {"path": "a.py", "old": "alpha", "new": "ALPHA"},
        {"path": "b.py", "old": "beta", "new": "BETA"},
    ]})
    assert r["ok"] and "changed on disk" in r["content"]
    assert "b.py" in r["content"].split("changed on disk", 1)[1]


def test_own_edits_and_unchanged_files_do_not_warn(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "one two\n"})
    ex.run("read_file", {"path": "a.txt"})
    r1 = ex.run("edit_file", {"path": "a.txt", "old": "one", "new": "ONE"})
    r2 = ex.run("edit_file", {"path": "a.txt", "old": "two", "new": "TWO"})  # no re-read
    assert r1["ok"] and r2["ok"]
    assert "changed on disk" not in r1["content"]            # unchanged file
    assert "changed on disk" not in r2["content"]            # own write != stale
    # never-read files have no baseline -> no warning either
    r3 = ex.run("write_file", {"path": "fresh.txt", "content": "x"})
    assert "changed on disk" not in r3["content"]


# --- read_file header graph enrichment (role + relations from the repo graph)

def test_read_header_enriched_with_graph_context(tmp_path):
    # Role + relations come from the LIVE repo graph (IntelService.index /
    # .graph) when present -- FACTS only: the symbols the file defines and the
    # files whose refs point at them. No tree-sitter needed here: the header
    # reads the same models/graph objects a real IntelService holds.
    from webbee.intel.models import ProjectIndex, FileIndex, Symbol
    from webbee.intel.graph import CodeGraph

    (tmp_path / "a.py").write_text("class Alpha:\n    pass\n\ndef helper():\n    pass\n")
    (tmp_path / "b.py").write_text("from a import Alpha\nAlpha()\n")
    idx = ProjectIndex(files={
        "a.py": FileIndex(path="a.py", lang="python", symbols=[
            Symbol("Alpha", "class", "a.py", 1, 2),
            Symbol("helper", "function", "a.py", 4, 5)]),
        "b.py": FileIndex(path="b.py", lang="python", refs=["Alpha"]),
    })
    class _Svc:  # duck-typed IntelService: .index + .graph is all the header reads
        index = idx
        graph = CodeGraph(idx)
    ex = LocalToolExecutor(str(tmp_path), indexer=_Svc())
    header = ex.run("read_file", {"path": "a.py"})["content"].split("\n", 1)[0]
    assert "defines Alpha, helper" in header
    assert "↔ used by b.py" in header
    assert "5 lines" in header                    # cheap metadata still present


def test_read_header_intel_degrades_gracefully(tmp_path):
    # indexer=None (base install), an unindexed file, and a BROKEN indexer all
    # degrade to the plain lines+mtime header -- never a crash, never an empty
    # "defines"/"used by" fabricated without graph data.
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "x = 1\n"})
    h = ex.run("read_file", {"path": "a.py"})["content"].split("\n", 1)[0]
    assert "1 line" in h and "defines" not in h and "↔" not in h

    class _Boom:
        @property
        def index(self):
            raise RuntimeError("intel down")
    ex2 = LocalToolExecutor(str(tmp_path), indexer=_Boom())
    r = ex2.run("read_file", {"path": "a.py"})
    assert r["ok"] and "1 line" in r["content"].split("\n", 1)[0]
