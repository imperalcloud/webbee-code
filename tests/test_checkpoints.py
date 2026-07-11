import os
import subprocess

from webbee.checkpoints import ShadowGit


def _sg(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    sg = ShadowGit(str(root), "rk_test", cache_dir=str(tmp_path / "cache"))
    assert sg.ensure()
    return sg, root


def _write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_ensure_creates_shadow_outside_workspace(tmp_path):
    sg, root = _sg(tmp_path)
    assert sg.available
    assert not str(sg.git_dir).startswith(str(root))       # shadow lives in the cache
    assert not (root / ".git").exists()                     # workspace untouched


def test_checkpoint_and_list(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "one")
    cp1 = sg.checkpoint("first")
    assert cp1 and cp1["changed"] and cp1["n"] == 1 and cp1["label"] == "first"
    _write(root, "a.txt", "two")
    cp2 = sg.checkpoint("second")
    assert cp2 and cp2["changed"] and cp2["n"] == 2
    rows = sg.list_checkpoints()
    assert [r["n"] for r in rows] == [2, 1]                 # newest first
    assert rows[0]["label"] == "second"


def test_checkpoint_no_changes_is_honest(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "one")
    sg.checkpoint("first")
    cp = sg.checkpoint("again")
    assert cp and cp["changed"] is False and cp["label"] == "(no changes)"
    assert len(sg.list_checkpoints()) == 1                  # no empty checkpoints


def test_rollback_restores_tracked_state(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "keep.txt", "v1")
    _write(root, "gone.txt", "will be deleted by the agent")
    cp = sg.checkpoint("good state")
    _write(root, "keep.txt", "WRECKED")
    (root / "gone.txt").unlink()
    _write(root, "junk.txt", "created after the checkpoint")
    sg.checkpoint("wrecked")
    r = sg.rollback(str(cp["n"]))
    assert r["ok"], r
    assert (root / "keep.txt").read_text(encoding="utf-8") == "v1"      # restored
    assert (root / "gone.txt").read_text(encoding="utf-8").startswith("will be")  # resurrected
    # junk.txt was checkpointed by "wrecked" -> the rollback removes it from
    # the worktree, but it is RECOVERABLE: the pre-rollback snapshot holds it.
    assert not (root / "junk.txt").exists()
    assert "undoable" in r["content"]                       # pre-rollback snapshot mentioned
    pre = sg.list_checkpoints()[0]
    assert pre["label"] == "pre-rollback"
    sg.rollback(str(pre["n"]))
    assert (root / "junk.txt").exists()                     # nothing is ever lost


def test_rollback_is_itself_undoable(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "v1")
    cp1 = sg.checkpoint("v1")
    _write(root, "a.txt", "v2")
    sg.checkpoint("v2")
    _write(root, "a.txt", "v3-uncommitted")
    sg.rollback(str(cp1["n"]))
    assert (root / "a.txt").read_text(encoding="utf-8") == "v1"
    rows = sg.list_checkpoints()                             # pre-rollback snapshot exists
    assert rows[0]["label"] == "pre-rollback"
    sg.rollback(str(rows[0]["n"]))                           # undo the rollback
    assert (root / "a.txt").read_text(encoding="utf-8") == "v3-uncommitted"


def test_resolve_accepts_n_cpn_and_sha(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "x")
    cp = sg.checkpoint("x")
    for ref in (str(cp["n"]), f"cp-{cp['n']}", cp["id"]):
        assert sg.rollback(ref)["ok"], ref


def test_rollback_unknown_ref_is_honest(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "x")
    sg.checkpoint("x")
    r = sg.rollback("cp-999")
    assert not r["ok"] and "unknown checkpoint" in r["content"]


def test_diff_reports_changes_and_caps(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "line1\n")
    sg.checkpoint("base")
    _write(root, "a.txt", "line1\nline2\n")
    out = sg.diff()
    assert "a.txt" in out and "+line2" in out
    sg._DIFF_CAP = 50
    big = "\n".join(f"row {i}" for i in range(200))
    _write(root, "big.txt", big)
    out2 = sg.diff()
    assert "truncated" in out2                              # honest cut, never silent


def test_gitignore_is_honored(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, ".gitignore", "node_modules/\n")
    _write(root, "node_modules/dep.js", "x" * 10)
    _write(root, "src.py", "code")
    sg.checkpoint("base")
    r = sg._git("ls-files")
    assert "src.py" in r.stdout and "node_modules/dep.js" not in r.stdout


def test_users_real_git_repo_untouched(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
    (root / "user.txt").write_text("user file", encoding="utf-8")
    head_before = (root / ".git" / "HEAD").read_bytes()
    sg = ShadowGit(str(root), "rk_real", cache_dir=str(tmp_path / "cache"))
    assert sg.ensure()
    sg.checkpoint("shadow snap")
    assert (root / ".git" / "HEAD").read_bytes() == head_before   # user refs untouched
    r = subprocess.run(["git", "-C", str(root), "log", "--oneline"],
                       capture_output=True, text=True)
    assert "shadow snap" not in (r.stdout + r.stderr)             # no shadow commit leaked


def test_no_git_binary_fails_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    sg = ShadowGit(str(tmp_path / "ws2"), "rk_nogit", cache_dir=str(tmp_path / "cache2"))
    os.makedirs(str(tmp_path / "ws2"), exist_ok=True)
    assert sg.ensure() is False and sg.available is False
    assert sg.checkpoint("x") is None
    assert not sg.rollback("1")["ok"]
    assert "unavailable" in sg.diff()


def test_acceptance_wrecked_project_recovers_in_one_rollback(tmp_path):
    """Spec §3.4 acceptance: 'a wrecked-project scenario is recoverable in one
    rollback'. The agent (via real tools + real shadow) builds a project,
    then wrecks it -- overwrite, delete, garbage -- and ONE rollback restores
    every checkpointed byte. The nagania answer: not a jail, a time machine."""
    from webbee.tools import LocalToolExecutor

    root = tmp_path / "proj"
    root.mkdir()
    sg = ShadowGit(str(root), "rk_acc", cache_dir=str(tmp_path / "cache"))
    assert sg.ensure()
    ex = LocalToolExecutor(str(root), shadow=sg)

    ex.run("write_file", {"path": "bot/main.py", "content": "def start():\n    return 'ok'\n"})
    ex.run("write_file", {"path": "bot/config.py", "content": "TOKEN = 'placeholder'\n"})
    ex.run("write_file", {"path": "README.md", "content": "# bot\n"})
    good = sg.checkpoint("good state")
    assert good and good["changed"]

    # The wreck: overwrite, delete, scatter garbage (each write auto-checkpoints).
    ex.run("write_file", {"path": "bot/main.py", "content": "TRASHED\n"})
    ex.run("bash", {"command": "rm bot/config.py"})
    ex.run("write_file", {"path": "bot/junk1.py", "content": "garbage\n"})
    ex.run("multi_edit", {"edits": [
        {"path": "README.md", "old": "# bot", "new": "# WRECKED"}]})

    r = ex.run("rollback", {"checkpoint": str(good["n"])})   # ONE rollback
    assert r["ok"], r
    assert (root / "bot/main.py").read_text(encoding="utf-8") == "def start():\n    return 'ok'\n"
    assert (root / "bot/config.py").read_text(encoding="utf-8") == "TOKEN = 'placeholder'\n"
    assert (root / "README.md").read_text(encoding="utf-8") == "# bot\n"
    assert not (root / "bot/junk1.py").exists()              # the wreck's garbage is gone too
    # And the rollback itself is undoable -- nothing was destroyed.
    rows = sg.list_checkpoints()
    assert rows[0]["label"] == "pre-rollback"


def test_rollback_refuses_without_safety_snapshot(tmp_path, monkeypatch):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "v1")
    cp = sg.checkpoint("v1")
    _write(root, "a.txt", "v2-PRECIOUS-UNCOMMITTED")
    monkeypatch.setattr(sg, "checkpoint", lambda label="", force=False: None)
    r = sg.rollback(str(cp["n"]))
    assert not r["ok"] and "refused" in r["content"]
    assert (root / "a.txt").read_text(encoding="utf-8") == "v2-PRECIOUS-UNCOMMITTED"


def test_git_env_leakage_is_scrubbed(tmp_path, monkeypatch):
    import subprocess as sp
    user = tmp_path / "userrepo"
    user.mkdir()
    sp.run(["git", "init", str(user)], capture_output=True, check=True)
    (user / "u.txt").write_text("user", encoding="utf-8")
    # A hook/rebase context exports these -- the shadow must ignore them.
    monkeypatch.setenv("GIT_INDEX_FILE", str(user / ".git" / "index"))
    monkeypatch.setenv("GIT_DIR", str(user / ".git"))
    sg = ShadowGit(str(user), "rk_leak", cache_dir=str(tmp_path / "cache"))
    assert sg.ensure()
    (user / "agent.txt").write_text("agent file", encoding="utf-8")
    assert sg.checkpoint("snap")["changed"]
    st = sp.run(["git", "-C", str(user), "status", "--porcelain"],
                capture_output=True, text=True,
                env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    assert "A " not in st.stdout                       # nothing staged in the USER index


def test_shadow_key_is_per_worktree(tmp_path):
    from webbee.checkpoints import shadow_key
    a = tmp_path / "clone-a"; a.mkdir()
    b = tmp_path / "clone-b"; b.mkdir()
    assert shadow_key(str(a)) != shadow_key(str(b))    # same remote, separate machines
    assert shadow_key(str(a)) == shadow_key(str(a))


def test_bare_repo_workspace_is_refused(tmp_path):
    import subprocess as sp
    bare = tmp_path / "bare"
    sp.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
    sg = ShadowGit(str(bare), "rk_bare", cache_dir=str(tmp_path / "cache"))
    assert sg.ensure() is False and sg.available is False


def test_failed_add_never_fakes_a_checkpoint(tmp_path, monkeypatch):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "x")
    real_git = sg._git
    def _fake(*args):
        if args and args[0] == "add":
            class _R: returncode = 1; stdout = ""; stderr = "add failed"
            return _R()
        return real_git(*args)
    monkeypatch.setattr(sg, "_git", _fake)
    assert sg.checkpoint("x") is None


def test_shadow_dir_is_private(tmp_path):
    sg, _root = _sg(tmp_path)
    assert (os.stat(sg.git_dir).st_mode & 0o777) == 0o700


def test_auto_latch_survives_a_single_transient_failure(tmp_path):
    sg, _root = _sg(tmp_path)
    sg.note_auto_result(False)          # one transient miss
    assert sg.auto_ok is True           # NOT disabled on a single failure
    sg.note_auto_result(True)           # a success clears the streak
    assert sg.auto_ok is True and sg._auto_fail_streak == 0


def test_auto_latch_trips_after_consecutive_failures(tmp_path):
    sg, _root = _sg(tmp_path)
    for _ in range(sg._AUTO_FAIL_LATCH):
        sg.note_auto_result(False)
    assert sg.auto_ok is False          # latched after N consecutive
    sg.note_auto_result(True)           # recovery re-enables
    assert sg.auto_ok is True


def test_describe_surfaces_paused_autocheckpoint(tmp_path):
    sg, root = _sg(tmp_path)
    _write(root, "a.txt", "x"); sg.checkpoint("c1")
    for _ in range(sg._AUTO_FAIL_LATCH):
        sg.note_auto_result(False)
    out = sg.describe()
    assert out.startswith("⚠ Auto-checkpointing is paused")
    assert "cp-1" in out                # the list still follows the warning
