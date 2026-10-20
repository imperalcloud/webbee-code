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


def test_grep_on_a_single_file(tmp_path):
    """live 2026-10-20: every grep whose path was one file answered "(no matches)"."""
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "app/writers.py", "content": "x = 1\n@writer\ndef save():\n    pass\n"})
    r = ex.run("grep", {"pattern": "@writer", "path": "app/writers.py"})
    assert r["ok"] and r["match_count"] == 1 and r["content"] == os.path.join("app", "writers.py") + ":2:@writer"
    assert ex.run("grep", {"pattern": "nothing", "path": "app/writers.py"})["content"] == "(no matches)"


def test_grep_skips_git_dir(tmp_path):
    # Real regression coverage on this (posix) host: a matching file sitting
    # inside .git/ must never surface in results.
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.py", "content": "needle\n"})
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "blob.txt").write_text("needle\n")
    r = ex.run("grep", {"pattern": "needle"})
    assert r["ok"] and "a.py" in r["content"] and ".git" not in r["content"]


def test_grep_prunes_heavy_dirs(tmp_path):
    # W6: a code grep must never crawl node_modules/vendor/dist/etc. — huge and
    # never what the user meant. A match in src surfaces; one in node_modules
    # does not (the walk is pruned, not just filtered).
    ex = _ex(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle\n")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("needle\n")
    r = ex.run("grep", {"pattern": "needle"})
    assert r["ok"]
    assert "a.py" in r["content"]
    assert "node_modules" not in r["content"]


def test_grep_skips_git_dir_windows_style():
    # The os.walk-driven .git skip in _t_grep normalizes dp with os.sep so it
    # also matches on Windows, where os.walk yields backslash paths (an
    # un-normalized check never matches "C:\repo\.git" there). os.walk on
    # this posix host never emits backslash paths, so that half of the fix
    # is unverifiable through a real filesystem walk here -- pin the
    # extracted pure predicate directly instead of faking a filesystem
    # assertion that would silently no-op on this host.
    from webbee.tools import _is_git_dir
    assert _is_git_dir(r"C:\repo\.git")
    assert _is_git_dir(r"C:\repo\.git\sub")
    assert _is_git_dir("/repo/.git")
    assert not _is_git_dir(r"C:\repo\src")

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
    from imperal_sdk.code_index import ProjectIndex, FileIndex, Symbol
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


# --------------------------------------------------------------------------
# 0.3.40 (I-STREAM-STEP-DIFF) -- write_file/edit_file/multi_edit now return a
# unified diff of what actually changed, so the terminal can show it right
# after the result line instead of the user having to open the file.
# --------------------------------------------------------------------------
def test_write_file_new_file_has_no_diff_noise_but_edit_does(tmp_path):
    ex = _ex(tmp_path)
    # A brand-new file has nothing to diff AGAINST in a meaningful way for a
    # first write (before="") -- still produces a proper +-only unified diff.
    r = ex.run("write_file", {"path": "a.txt", "content": "line one\nline two\n"})
    assert r["ok"]
    assert "diff" in r
    assert "+line one" in r["diff"]
    assert "+line two" in r["diff"]


def test_write_file_overwrite_shows_before_and_after(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "old content\n"})
    r = ex.run("write_file", {"path": "a.txt", "content": "new content\n"})
    assert r["ok"]
    assert "-old content" in r["diff"]
    assert "+new content" in r["diff"]


def test_write_file_identical_content_produces_no_diff_key(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "same\n"})
    r = ex.run("write_file", {"path": "a.txt", "content": "same\n"})
    assert r["ok"]
    assert "diff" not in r


def test_edit_file_diff_shows_the_one_line_change(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "hello world\n"})
    r = ex.run("edit_file", {"path": "a.txt", "old": "world", "new": "webbee"})
    assert r["ok"]
    assert "-hello world" in r["diff"]
    assert "+hello webbee" in r["diff"]


def test_multi_edit_diff_covers_every_touched_file_once(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "foo\n"})
    ex.run("write_file", {"path": "b.txt", "content": "bar\n"})
    r = ex.run("multi_edit", {"edits": [
        {"path": "a.txt", "old": "foo", "new": "FOO"},
        {"path": "b.txt", "old": "bar", "new": "BAR"},
    ]})
    assert r["ok"]
    assert "diff" in r
    assert "a.txt" in r["diff"] and "b.txt" in r["diff"]
    assert "+FOO" in r["diff"] and "+BAR" in r["diff"]


def test_multi_edit_same_file_diff_is_one_clean_before_after(tmp_path):
    """Two edits to the SAME file in one batch -> ONE diff for that file
    (first-seen content vs. final), not one fragment per edit."""
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "one two three\n"})
    r = ex.run("multi_edit", {"edits": [
        {"path": "a.txt", "old": "one", "new": "ONE"},
        {"path": "a.txt", "old": "three", "new": "THREE"},
    ]})
    assert r["ok"]
    # Exactly one diff hunk-set for a.txt, not two separate --- a.txt blocks.
    assert r["diff"].count("--- a.txt") == 1
    assert "+ONE two THREE" in r["diff"]


def test_diff_is_capped_and_never_explodes_a_giant_rewrite(tmp_path):
    ex = _ex(tmp_path)
    ex.run("write_file", {"path": "a.txt", "content": "x\n" * 5})
    big = "y\n" * 20000
    r = ex.run("write_file", {"path": "a.txt", "content": big})
    assert r["ok"]
    assert "diff" in r
    assert len(r["diff"]) <= 4000 + 100   # _DIFF_CAP + truncation marker slack
    assert "truncated" in r["diff"]


# --- I-TOOLS-HONEST-TRUNCATION ------------------------------------------------
# Every unbounded tool caps its output AND says so. Regression for the live
# audit (2026-08-06): `bash` returned 200,001 chars uncapped, and `grep`
# returned exactly 200 of 500 real matches with NO marker -- the agent then
# reasoned from a half-truth believing it had seen everything.

def test_bash_output_is_capped_with_an_honest_marker(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("bash", {"command": "python3 -c \"print('X'*200000)\""})
    assert len(r["content"]) < 40000, "bash output must not be unbounded"
    assert "TRUNCATED" in r["content"]
    assert "200,000" in r["content"] or "200,001" in r["content"], "must state the real size"


def test_bash_short_output_is_never_marked(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("bash", {"command": "echo hello"})
    assert "hello" in r["content"]
    assert "TRUNCATED" not in r["content"]


def test_grep_states_how_many_matches_were_hidden(tmp_path):
    for i in range(300):
        (tmp_path / f"f{i}.py").write_text("MATCHME = 1\n")
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("grep", {"pattern": "MATCHME"})
    assert "TRUNCATED" in r["content"]
    assert "300" in r["content"], "must state the true total, not just the shown count"
    assert r["shown"] == 200 and r["match_count"] == 300
    assert r["truncated"] is True


def test_grep_under_the_cap_is_not_marked(tmp_path):
    (tmp_path / "a.py").write_text("MATCHME = 1\n")
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("grep", {"pattern": "MATCHME"})
    assert "TRUNCATED" not in r["content"]
    assert r["truncated"] is False


# --- I-TOOLS-PARTIAL-READ -----------------------------------------------------

def test_read_file_window_returns_exactly_that_window(tmp_path):
    (tmp_path / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 3001)))
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_file", {"path": "big.py", "offset": 400, "limit": 5})
    body = r["content"].split("\n", 1)[1]
    assert body.splitlines() == [f"line {i}" for i in range(400, 405)]
    assert (r["shown_from"], r["shown_to"], r["total_lines"]) == (400, 404, 3000)


def test_read_file_without_a_window_auto_caps_a_huge_file(tmp_path):
    (tmp_path / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 3001)))
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_file", {"path": "big.py"})
    assert r["truncated"] is True and r["shown_to"] == 2000
    assert "offset/limit" in r["content"].splitlines()[0]


def test_read_file_small_file_is_unchanged_and_unmarked(tmp_path):
    (tmp_path / "s.py").write_text("a = 1\nb = 2\n")
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_file", {"path": "s.py"})
    assert r["truncated"] is False
    assert r["content"].split("\n", 1)[1] == "a = 1\nb = 2\n"  # byte-exact, trailing NL kept


def test_read_file_offset_past_eof_is_empty_not_a_crash(tmp_path):
    (tmp_path / "s.py").write_text("a = 1\n")
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_file", {"path": "s.py", "offset": 999, "limit": 10})
    assert r["ok"] is True and r["shown_to"] >= r["shown_from"] - 1


def test_read_file_garbage_offset_limit_degrade_to_full_read(tmp_path):
    (tmp_path / "s.py").write_text("a = 1\n")
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_file", {"path": "s.py", "offset": "abc", "limit": None})
    assert r["ok"] is True and "a = 1" in r["content"]


# --- list_dir -----------------------------------------------------------------

def test_list_dir_lists_and_prunes_dependency_dirs(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "node_modules" / "junk").mkdir(parents=True)
    (tmp_path / "node_modules" / "junk" / "b.js").write_text("x\n")
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("list_dir", {"path": "."})
    assert "a.py" in r["content"] and "sub/" in r["content"]
    assert "node_modules" not in r["content"]


def test_list_dir_on_a_file_is_an_honest_error(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("list_dir", {"path": "a.py"})
    assert r["ok"] is False and "not a directory" in r["content"]


# ── code graph everywhere (plan 3 Task 5): the user's own SSH feeds the vault's graph ──

class _E:
    def __init__(self, name, kind, size=10):
        self.filename = name
        self.attrs = type("A", (), {"type": kind, "size": size, "mtime": 3.0})()


class _F:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self, n=-1):
        return self.data


class _Sftp:
    def __init__(self, tree, files):
        self.tree, self.files, self.reads = tree, files, []

    async def readdir(self, p):
        if p not in self.tree:
            raise OSError(p)
        return self.tree[p]

    def open(self, p, mode="rb"):
        self.reads.append(p)
        if p not in self.files:
            raise OSError(p)
        return _F(self.files[p])


class _Cloud:
    def __init__(self):
        self.calls = []

    async def _request(self, method, path, json=None, timeout=60):
        self.calls.append((path, json))
        if path.endswith("/sync/plan"):
            return {"repo_key": "rk-remote", "changed": [i["path"] for i in json["listing"]], "removed": [],
                    "have_blobs": []}
        return {"files": len(json["files"])}


WIN_TREE = {"/C:/u/proj": [_E(".git", 2), _E("Pkg", 2), _E(".env", 1), _E("setup.py", 1)],
            "/C:/u/proj/Pkg": [_E("Foo.py", 1), _E("Bar.py", 1)]}
WIN_FILES = {"/C:/u/proj/Pkg/Foo.py": b"def foo():\r\n    pass\r\n", "/C:/u/proj/Pkg/Bar.py": b"def foo():\n    pass\n",
             "/C:/u/proj/.git/HEAD": b"ref: refs/heads/main\n", "/C:/u/proj/.env": b"S=1", "/C:/u/proj/setup.py": b"x"}


def _remote(monkeypatch, sftp, fps=("SHA256:win",), why=""):
    import webbee.tools as t

    async def fake_open(host):
        return (sftp if not why else None), None, list(fps), why
    monkeypatch.setattr(t, "_open_sftp", fake_open)
    cloud = _Cloud()
    ex = LocalToolExecutor("/tmp", client_factory=lambda: cloud)
    return ex, cloud


def test_a_windows_server_repo_syncs_with_canonical_paths_and_one_blob(monkeypatch):
    sftp = _Sftp(WIN_TREE, WIN_FILES)
    ex, cloud = _remote(monkeypatch, sftp)
    res = ex.run("intel_sync_remote", {"host": "winbox", "root": "/C:/u/proj/Pkg"})
    assert res["ok"] and res["content"]["repo_key"] == "rk-remote"
    plan = cloud.calls[0][1]
    assert plan["meta"]["os"] == "windows" and plan["meta"]["root"] == "/C:/u/proj"
    assert plan["meta"]["source"] == "system_ssh" and plan["meta"]["ref"] == "winbox"
    assert plan["meta"]["machine"]["fingerprints"] == ["SHA256:win"]
    assert sorted(i["path"] for i in plan["listing"]) == ["Pkg/Bar.py", "Pkg/Foo.py"]
    applied = cloud.calls[1][1]
    assert len(applied["blobs"]) == 1 and len(applied["files"]) == 2              # CRLF == LF: one version
    assert not any(p.endswith(("setup.py", ".env")) for p in sftp.reads)          # never read


def test_a_passphrase_key_without_a_master_says_needs_interactive_login_without_prompting(monkeypatch):
    import asyncssh
    import webbee.tools as t
    monkeypatch.setattr(t, "master_open", lambda host: False)

    async def refuse(host, **kw):
        assert kw.get("passphrase") is None                                      # never asks
        raise asyncssh.KeyEncryptionError("passphrase required")
    monkeypatch.setattr(asyncssh, "connect", refuse)
    cloud = _Cloud()
    res = LocalToolExecutor("/tmp", client_factory=lambda: cloud).run("intel_sync_remote", {"host": "h", "root": "/r"})
    assert res["ok"] is False and res["content"]["partial_reason"] == "needs interactive login" and not cloud.calls


def test_the_ssh_config_alias_and_proxyjump_go_to_asyncssh(tmp_path, monkeypatch):
    import asyncssh
    import webbee.tools as t
    (tmp_path / ".ssh").mkdir()
    cfg = tmp_path / ".ssh" / "config"
    cfg.write_text("Host whm-jump\n  HostName 10.0.0.5\n  User root\n  ProxyJump bastion\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(t, "master_open", lambda host: False)
    seen = {}

    async def connect(host, **kw):
        seen.update(kw, host=host)
        raise OSError("stop here")
    monkeypatch.setattr(asyncssh, "connect", connect)
    LocalToolExecutor("/tmp", client_factory=lambda: _Cloud()).run("intel_sync_remote", {"host": "whm-jump"})
    assert seen["host"] == "whm-jump" and seen["config"] == [str(cfg)]
    opts = asyncssh.SSHClientConnectionOptions(config=[str(cfg)], host="whm-jump")
    assert opts.host == "10.0.0.5" and opts.username == "root"                     # the alias resolves to the real host


def test_an_open_master_connection_reads_over_the_system_sftp_batch(monkeypatch):
    import webbee.tools as t
    monkeypatch.setattr(t, "master_open", lambda host: True)
    monkeypatch.setattr(t, "known_fingerprints", lambda host: ["SHA256:m"])
    batches = []

    async def batch(self, commands):
        batches.append(commands)
        if commands.startswith("ls -la"):
            return {'ls -la "/r"\n': "drwxr-xr-x 3 u u 96 Jan 2 10:00 /r/.git\n-rw-r--r-- 1 u u 9 Jan 2 10:00 /r/a.py\n"}\
                .get(commands, "")
        local = commands.rsplit('"', 2)[-2]
        with open(local, "wb") as f:
            f.write(b"x = 1\n")
        return ""
    monkeypatch.setattr(t.SystemSftp, "_batch", batch)
    cloud = _Cloud()
    res = LocalToolExecutor("/tmp", client_factory=lambda: cloud).run("intel_sync_remote", {"host": "h", "root": "/r"})
    assert res["ok"] and any(b.startswith("get ") for b in batches)
    assert cloud.calls[0][1]["meta"]["machine"]["fingerprints"] == ["SHA256:m"]


def test_the_sync_verb_is_a_read_the_client_never_prompts_for():
    import asyncio
    from webbee.consent import handle_confirm_request
    asked = []

    async def ask(*a):
        asked.append(a)
        return "no"
    out = asyncio.run(handle_confirm_request({"req_id": "r1", "tool": "intel_sync_remote", "args": {}}, "ask", ask))
    assert out["result"] == {"approved": True} and not asked


def test_system_sftp_answers_lstat_and_realpath_from_the_server():
    """Security (sdk 6.3.1): the sdk's reads check lstat + realpath -- the sftp -b adapter answers both
    (a link from its directory listing, the resolved path from the server's cd + pwd), cached per path."""
    import asyncio
    from webbee.tools import SystemSftp
    calls = []

    async def batch(cmds):
        calls.append(cmds)
        if cmds.startswith("ls -la"):
            return ("drwxr-xr-x  3 u u 4096 Oct 19 10:00 .\n"
                    "lrwxrwxrwx  1 u u   11 Oct 19 10:00 cfg.toml -> /etc/passwd\n"
                    "-rw-r--r--  1 u u   10 Oct 19 10:00 a.py\n")
        if '"/r/out"' in cmds:
            return "Remote working directory: /etc\n"
        if '"/r/a.py"' in cmds:
            raise OSError("not a directory")
        return "Remote working directory: /r\n"
    s = SystemSftp("whm-jump")
    s._batch = batch
    assert asyncio.run(s.lstat("/r/cfg.toml")).type == 3 and asyncio.run(s.lstat("/r/a.py")).type == 1
    assert asyncio.run(s.realpath("/r/out")) == "/etc"
    assert asyncio.run(s.realpath("/r/a.py")) == "/r/a.py"
    n = len(calls)
    asyncio.run(s.realpath("/r/a.py"))
    asyncio.run(s.lstat("/r/a.py"))
    assert len(calls) == n                                    # cached: no new sftp process


def test_system_sftp_refuses_a_control_character_in_a_remote_path():
    """Security review of 0.5.0: a newline would start a second sftp batch command ("!" runs a local shell)."""
    import asyncio
    import pytest
    from webbee.tools import SystemSftp
    ran = []

    async def batch(cmds):
        ran.append(cmds)
        return ""
    s = SystemSftp("h")
    s._batch = batch
    for bad in ("/r/a.py\n!touch /tmp/pwned", "/r/a\rb", "/r/\x00x"):
        with pytest.raises(OSError):
            asyncio.run(s.readdir(bad))
    assert ran == []


def test_an_asyncssh_connect_reports_every_key_of_the_host_the_user_trusts(monkeypatch):
    """Live 2026-10-20: the panel met whm-jump by RSA, the user's ssh by ed25519 -- two machine nodes. The
    client sends the negotiated key and the host's other known_hosts keys: one machine."""
    import asyncio
    import asyncssh
    import webbee.tools as t
    monkeypatch.setattr(t, "master_open", lambda host: False)
    monkeypatch.setattr(t, "known_fingerprints", lambda host: ["SHA256:rsa", "SHA256:ecdsa", "SHA256:ed"])

    class Key:
        def get_fingerprint(self, alg):
            return "SHA256:ed"

    class Conn:
        def get_server_host_key(self):
            return Key()

        async def start_sftp_client(self):
            return "sftp"

    async def connect(host, **kw):
        return Conn()
    monkeypatch.setattr(asyncssh, "connect", connect)
    sftp, conn, fps, why = asyncio.run(t._open_sftp("whm-jump"))
    assert sftp == "sftp" and why == "" and fps == ["SHA256:ecdsa", "SHA256:ed", "SHA256:rsa"]
