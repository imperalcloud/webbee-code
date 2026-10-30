"""Code review C3 (2026-10-30): nested repositories are snapshotted and rolled back file by file.

`git add -A` stored each nested repo as a gitlink with none of its files, so a rollback in a folder of
repositories restored none of the agent's edits while saying it restored the workspace."""
import os
import subprocess


from webbee import checkpoints
from webbee.checkpoints import ShadowGit

_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=_ENV)


def _repo(path, files):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    for rel, text in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("-c", "user.name=u", "-c", "user.email=u@x", "-c", "commit.gpgsign=false",
         "commit", "-qm", "user commit", cwd=path)


def _user_state(path):
    """What a checkpoint/rollback must never change in the user's repo: HEAD, refs and the index bytes."""
    g = path / ".git"
    return ((g / "HEAD").read_bytes(), (g / "index").read_bytes(),
            _git("rev-parse", "HEAD", cwd=path).stdout)


def _sg(root, tmp_path):
    sg = ShadowGit(str(root), "rk_nested", cache_dir=str(tmp_path / "cache"))
    assert sg.ensure()
    return sg


def test_folder_of_two_repos_rolls_back_both(tmp_path):
    ws = tmp_path / "workspace"
    gw = ws / "wpanel" / "core" / "gateway"
    mod = ws / "wpanel" / "modules" / "x"
    _repo(gw, {"app.py": "v0\n"})
    _repo(mod, {"lib/m.py": "v0\n"})
    before = (_user_state(gw), _user_state(mod))
    sg = _sg(ws, tmp_path)

    (gw / "app.py").write_text("v1\n", encoding="utf-8")
    (mod / "lib" / "m.py").write_text("v1\n", encoding="utf-8")
    cp = sg.checkpoint("good")
    assert cp and cp["changed"]
    assert set(cp["nested"]) == {"wpanel/core/gateway", "wpanel/modules/x"}
    assert "wpanel/core/gateway/app.py" in cp["paths"] and "wpanel/modules/x/lib/m.py" in cp["paths"]
    # stored as files, never as a gitlink
    tree = sg._git("ls-tree", "-r", "HEAD").stdout
    assert "160000" not in tree and "wpanel/core/gateway/app.py" in tree

    (gw / "app.py").write_text("WRECKED\n", encoding="utf-8")
    (mod / "lib" / "m.py").write_text("WRECKED\n", encoding="utf-8")
    (gw / "junk.py").write_text("garbage\n", encoding="utf-8")
    r = sg.rollback(str(cp["n"]))
    assert r["ok"], r
    assert (gw / "app.py").read_text(encoding="utf-8") == "v1\n"
    assert (mod / "lib" / "m.py").read_text(encoding="utf-8") == "v1\n"
    assert not (gw / "junk.py").exists()                  # never in the checkpoint: removed, kept in pre-rollback
    assert "wpanel/core/gateway" in r["content"] and "wpanel/modules/x" in r["content"]
    assert "NOT restored" not in r["content"]
    # the user's own repos: HEAD, refs and index bytes untouched by checkpoint and rollback
    assert (_user_state(gw), _user_state(mod)) == before
    assert (gw / ".git").is_dir() and (mod / ".git").is_dir()

    # the rollback is itself undoable, inside nested repos too
    pre = sg.list_checkpoints()[0]
    assert pre["label"] == "pre-rollback"
    assert sg.rollback(str(pre["n"]))["ok"]
    assert (gw / "app.py").read_text(encoding="utf-8") == "WRECKED\n"
    assert (gw / "junk.py").exists()


def test_nested_repo_inside_a_repo_is_restored(tmp_path):
    ws = tmp_path / "proj"
    _repo(ws, {"top.py": "t0\n"})
    inner = ws / "vendor" / "lib"
    _repo(inner, {"inner.py": "i0\n"})
    deeper = inner / "plugins" / "p"
    _repo(deeper, {"deep.py": "d0\n"})            # a repo inside the nested repo
    before = (_user_state(ws), _user_state(inner), _user_state(deeper))
    status_before = [_git("status", "--porcelain", cwd=d).stdout for d in (ws, inner, deeper)]
    sg = _sg(ws, tmp_path)

    cp = sg.checkpoint("base")
    assert cp and "vendor/lib" in cp["nested"] and "vendor/lib/plugins/p" in cp["nested"]
    (ws / "top.py").write_text("X\n", encoding="utf-8")
    (inner / "inner.py").write_text("X\n", encoding="utf-8")
    (deeper / "deep.py").write_text("X\n", encoding="utf-8")
    sg.checkpoint("wreck")
    r = sg.rollback(str(cp["n"]))
    assert r["ok"], r
    assert (ws / "top.py").read_text(encoding="utf-8") == "t0\n"
    assert (inner / "inner.py").read_text(encoding="utf-8") == "i0\n"
    assert (deeper / "deep.py").read_text(encoding="utf-8") == "d0\n"
    assert (_user_state(ws), _user_state(inner), _user_state(deeper)) == before
    # the user's repos report exactly what they did before: nothing staged, the files clean again
    assert [_git("status", "--porcelain", cwd=d).stdout for d in (ws, inner, deeper)] == status_before


def test_nested_gitignore_and_dependency_dirs_are_left_out(tmp_path):
    ws = tmp_path / "ws"
    app = ws / "app"
    _repo(app, {".gitignore": "secret.env\n", "main.py": "m\n"})
    (app / "secret.env").write_text("x", encoding="utf-8")
    (app / "node_modules" / "dep").mkdir(parents=True)
    (app / "node_modules" / "dep" / "i.js").write_text("x", encoding="utf-8")
    (ws / "env" / "lib").mkdir(parents=True)
    (ws / "env" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (ws / "env" / "lib" / "site.py").write_text("x", encoding="utf-8")
    (ws / "pkg" / "__pycache__").mkdir(parents=True)
    (ws / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"x")
    sg = _sg(ws, tmp_path)
    cp = sg.checkpoint("base")
    files = sg._git("ls-files").stdout.split()
    assert "app/main.py" in files and "app/.gitignore" in files
    assert not any(("node_modules" in f or "__pycache__" in f or f.startswith("env/") or f.endswith("secret.env"))
                   for f in files), files
    assert ("env/", "a Python virtualenv (pyvenv.cfg)") in cp["uncovered"]


def test_large_file_is_named_and_never_overwritten(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "small.txt").write_text("s0", encoding="utf-8")
    (ws / "big.bin").write_bytes(b"a" * 10)
    sg = _sg(ws, tmp_path)
    cp = sg.checkpoint("base")                         # big.bin still small: it is in this checkpoint
    assert cp["uncovered"] == []
    monkeypatch.setattr(checkpoints, "_MAX_FILE_BYTES", 100)
    (ws / "big.bin").write_bytes(b"b" * 500)            # grows past the bound: the safety snapshot cannot hold it
    (ws / "small.txt").write_text("s1", encoding="utf-8")
    r = sg.rollback(str(cp["n"]))
    assert r["ok"], r
    assert (ws / "small.txt").read_text(encoding="utf-8") == "s0"
    assert (ws / "big.bin").read_bytes() == b"b" * 500  # NOT overwritten with the old copy
    assert "big.bin" in r["left_as_is"]
    assert "big.bin -- larger than" in r["content"]
    pre = sg.list_checkpoints()[0]
    assert "not snapshotted: big.bin" in sg._git("log", "-1", "--format=%b", "refs/webbee/cp-%d" % pre["n"]).stdout
    # a rollback TO a checkpoint that left a file out says so
    r2 = sg.rollback(str(pre["n"]))
    assert r2["ok"] and "NOT restored" in r2["content"] and "big.bin" in r2["not_restored"]


def test_old_gitlink_checkpoint_is_named_and_keeps_the_nested_repo(tmp_path):
    """A checkpoint taken by the old `add -A` holds a nested repo only as a gitlink: rolling back to it must not
    delete the nested repo's files, and must say that repo was not restored."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sub = ws / "sub"
    _repo(sub, {"s.py": "s0\n"})
    (ws / "top.txt").write_text("t0", encoding="utf-8")
    sg = _sg(ws, tmp_path)
    assert sg._git("add", "-A").returncode == 0          # what the old snapshot did
    assert sg._git("commit", "-qm", "old style").returncode == 0
    sg._git("update-ref", "refs/webbee/cp-1", "HEAD")
    assert "160000" in sg._git("ls-tree", "-r", "HEAD").stdout

    (ws / "top.txt").write_text("t1", encoding="utf-8")
    (sub / "s.py").write_text("s1\n", encoding="utf-8")
    sg.checkpoint("new")                                 # replaces the gitlink by the files
    assert "160000" not in sg._git("ls-tree", "-r", "HEAD").stdout
    r = sg.rollback("1")
    assert r["ok"], r
    assert (ws / "top.txt").read_text(encoding="utf-8") == "t0"
    assert (sub / "s.py").read_text(encoding="utf-8") == "s1\n"    # kept, not deleted
    assert "sub/" in r["not_restored"] and "NOT restored" in r["content"]
    assert "pointer" in r["content"]


def test_unlistable_nested_repo_is_named(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _repo(ws / "a", {"f.py": "x\n"})
    sg = _sg(ws, tmp_path)
    monkeypatch.setattr(sg, "_nested_ls", lambda rel, deadline: ([], "fatal: detected dubious ownership"))
    cp = sg.checkpoint("x")
    assert ("a/", "nested repository its git could not list (fatal: detected dubious ownership)") in cp["uncovered"]


def test_slow_snapshot_pauses_auto_at_once(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    sg = _sg(ws, tmp_path)
    monkeypatch.setattr(checkpoints, "_SNAPSHOT_BUDGET_S", -1.0)   # already past the budget
    assert sg.checkpoint("pre:bash") is None
    assert sg.auto_ok is False and "longer than" in sg.pause_reason
    assert "longer than" in sg.describe()
    assert not os.path.exists(os.path.join(sg.git_dir, "index.lock"))
    monkeypatch.setattr(checkpoints, "_SNAPSHOT_BUDGET_S", 30.0)
    assert sg.checkpoint("manual")["changed"]                       # a manual checkpoint still works
