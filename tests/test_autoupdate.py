"""Owner 2026-10-25: webbee starts on the newest release of itself and of the shell's imperal-mcp (with its code
extra), on any system -- the upgrade first, then the start."""
import os
import subprocess

import pytest

from webbee import autoupdate as au


@pytest.fixture(autouse=True)
def _posix_restart(monkeypatch):
    """These pin the POSIX restart (exec); the Windows one (run the new release, pass its exit code on) has its own
    test below -- on a Windows runner every test here took that path and ran a real child (os-check 2026-11-14)."""
    monkeypatch.setattr(au, "_WINDOWS", False)


class _Run:
    def __init__(self, rc=0, stdout=""):
        self.calls, self.rc, self.stdout = [], rc, stdout

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.rc if "install" in cmd else 0,
                                           stdout=self.stdout if "-c" in cmd else "", stderr="")


def _which(found):
    return lambda name: found.get(name)


def test_the_installer_is_read_from_the_install_s_path():
    assert au.tool_of("/Users/me/.local/share/uv/tools/webbee") == "uv"
    assert au.tool_of(r"C:\Users\me\AppData\Roaming\uv\tools\imperal-mcp\Scripts\imperal-mcp.exe") == "uv"
    assert au.tool_of("/home/me/.local/pipx/venvs/webbee") == "pipx"
    assert au.tool_of("/usr/lib/python3/site-packages") == "pip"


def test_webbee_upgrades_itself_by_its_own_tool():
    uv = au.self_command("0.6.0", prefix="/u/.local/share/uv/tools/webbee", env={}, uv="/bin/uv")
    assert uv == ["/bin/uv", "tool", "install", "--force", "--refresh", "webbee==0.6.0"]
    px = au.self_command("0.6.0", prefix="/h/.local/pipx/venvs/webbee", env={}, pipx="/bin/pipx")
    assert px == ["/bin/pipx", "runpip", "webbee", "install", "--upgrade", "--no-cache-dir", "webbee==0.6.0"]
    pip = au.self_command("0.6.0", prefix="/venv", executable="/venv/bin/python", env={})
    assert pip == ["/venv/bin/python", "-m", "pip", "install", "--upgrade", "--no-cache-dir", "webbee==0.6.0"]
    light = au.self_command("0.6.0", env={au.LIGHT_ENV: "/usr/bin/python3.8"})      # an old Python's light install
    assert light == ["/usr/bin/python3.8", "-m", "pip", "install", "--upgrade", "--no-cache-dir", "webbee==0.6.0"]


def test_a_newer_webbee_is_installed_and_started_again_before_anything_else():
    run, execd, env = _Run(), [], {}
    fetch = {"webbee": "0.6.0", "imperal-mcp": "0.8.2"}.get
    au.upgrade_before_start(["--mode", "plan"], "0.5.14", say=lambda m: None, fetch=fetch, wheel=None,
                            which=_which({"pipx": "/bin/pipx"}), run=run, execv=lambda p, a: execd.append(a),
                            env=env)
    assert run.calls[0][-1] == "webbee==0.6.0" and env[au.DONE_ENV] == "1"
    assert execd and execd[0][-2:] == ["--mode", "plan"]


def test_the_shell_s_imperal_mcp_is_brought_to_the_newest_with_its_code_extra(tmp_path, monkeypatch):
    exe = tmp_path / "uv" / "tools" / "imperal-mcp" / "bin" / "imperal-mcp"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    run = _Run(stdout="0.8.1 False")                          # an old release that lost its code extra
    monkeypatch.setattr(au, "find_uv", lambda which=None: "/bin/uv")
    did = au.upgrade_before_start([], "0.5.14", say=lambda m: None, wheel=None,
                                  fetch={"webbee": "0.5.14", "imperal-mcp": "0.8.2"}.get,
                                  which=_which({"imperal-mcp": str(exe)}), run=run, env={})
    assert ["/bin/uv", "tool", "install", "--force", "--refresh", "imperal-mcp[code]==0.8.2"] in run.calls
    assert did == ["imperal-mcp 0.8.1 -> 0.8.2"]


def test_nothing_changes_when_all_is_newest_or_pypi_is_out_of_reach(tmp_path, monkeypatch):
    exe = tmp_path / "pipx" / "venvs" / "imperal-mcp" / "bin" / "imperal-mcp"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(au, "find_uv", lambda which=None: None)
    for fetch in ({"webbee": "0.5.14", "imperal-mcp": "0.8.2"}.get, lambda d: None):
        run = _Run(stdout="0.8.2 True")
        au.upgrade_before_start([], "0.5.14", wheel=lambda d, v: None, say=lambda m: None, fetch=fetch,
                                which=_which({"imperal-mcp": str(exe), "pipx": "/bin/pipx"}), run=run, env={})
        assert not any("install" in c for c in run.calls)


def test_a_missing_imperal_mcp_is_installed_from_git_when_pypi_is_out_of_reach():
    assert au.mcp_command({}, None, "/bin/uv", None) == ["/bin/uv", "tool", "install", "--force", "--refresh", au.MCP_GIT_SPEC]
    assert au.mcp_command({}, "0.8.2", None, "/bin/pipx")[-1] == "imperal-mcp[code]==0.8.2"
    assert au.mcp_command({}, "0.8.2", None, None) is None             # no uv, no pipx: nothing to install with


def test_a_failed_or_switched_off_upgrade_never_stops_the_start(monkeypatch):
    said = []
    monkeypatch.setattr(au, "find_uv", lambda which=None: "/bin/uv")
    did = au.upgrade_before_start([], "0.5.14", wheel=lambda d, v: None, say=said.append, fetch={"webbee": "0.6.0", "imperal-mcp": None}.get,
                                  which=_which({}), run=_Run(rc=1), execv=lambda *a: 1 / 0,
                                  env={})
    assert did == [] and any("could not update itself" in s for s in said)
    assert au.upgrade_before_start([], "0.5.14", wheel=lambda d, v: None, fetch=lambda d: 1 / 0, env={au.OFF_ENV: "1"}) == []
    run = _Run()                                    # just restarted after a self-upgrade: never a second one
    au.upgrade_before_start([], "0.5.14", wheel=lambda d, v: None, say=said.append, fetch={"webbee": "0.6.0"}.get, which=_which({}),
                            run=run, execv=lambda *a: 1 / 0, env={au.DONE_ENV: "1"})
    assert not any("webbee==0.6.0" in c for c in run.calls)


def test_no_program_or_module_is_taken_from_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert au.safe_which("x", which=lambda n: str(tmp_path / "x.exe")) is None       # planted beside the workspace
    assert au.safe_which("x", which=lambda n: "/usr/bin/x") == "/usr/bin/x"
    again = au.restart_command(["--once"], env={}, executable="/v/bin/python")
    assert again[2].startswith(au._NO_CWD) and again[-1] == "--once"


def test_one_odd_answer_never_reinstalls_and_a_silent_install_is_left_alone(tmp_path):
    exe = tmp_path / "uv" / "tools" / "imperal-mcp" / "bin" / "imperal-mcp"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    answers = iter(["0.8.1 False", "0.8.1 True"])

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=next(answers), stderr="")
    inst = au.mcp_installed(which=lambda n: str(exe), run=run)
    assert inst["code"] is True and au.mcp_command(inst, "0.8.1", "/bin/uv", None) is None
    assert au.mcp_command({"tool": "uv", "version": "", "code": False}, "0.8.2", "/bin/uv", None) is None


def test_a_release_is_installed_by_its_wheel_s_address_when_pypi_names_it():
    url = "https://files.pythonhosted.org/packages/a8/1c/x/webbee-0.6.1-py3-none-any.whl"
    wheel = lambda dist, v: url if dist == "webbee" else None                      # noqa: E731
    cmd = au.self_command("0.6.1", prefix="/h/.local/pipx/venvs/webbee", env={}, pipx="/bin/pipx", wheel=wheel)
    assert cmd[-1] == f"webbee @ {url}"
    mcp_url = "https://files.pythonhosted.org/packages/b/imperal_mcp-0.8.2-py3-none-any.whl"
    got = au.mcp_command({}, "0.8.2", "/bin/uv", None, wheel=lambda d, v: mcp_url)
    assert got[-1] == f"imperal-mcp[code] @ {mcp_url}"
    assert au.self_command("0.6.1", prefix="/v", executable="/v/bin/python", env={}, wheel=lambda d, v: None)[-1] \
        == "webbee==0.6.1"


# ── live 2026-10-25: two starts reinstalled one venv at once; the restart found it half-written ──
def test_pipx_upgrades_its_venv_in_place():
    px = au.self_command("0.6.5", prefix="/h/.local/pipx/venvs/webbee", env={}, pipx="/bin/pipx")
    assert px == ["/bin/pipx", "runpip", "webbee", "install", "--upgrade", "--no-cache-dir", "webbee==0.6.5"]
    inst = {"tool": "pipx", "python": "/p/bin/python", "version": "0.8.2", "code": True}
    assert au.mcp_command(inst, "0.8.3", None, "/bin/pipx") == [
        "/bin/pipx", "runpip", "imperal-mcp", "install", "--upgrade", "--no-cache-dir", "imperal-mcp[code]==0.8.3"]
    assert au.mcp_command({}, "0.8.3", None, "/bin/pipx")[:3] == ["/bin/pipx", "install", "--force"]   # a new one


def test_a_second_start_waits_for_the_first_upgrade_and_starts_on_it(tmp_path):
    lock = tmp_path / "webbee-upgrade.lock"
    # another webbee is upgrading right now: a process that runs on any system (pid 1 is init on POSIX, none on
    # Windows -- there the lock was rightly found dead and taken, os-check 2026-11-14)
    lock.write_text(f"{os.getppid()} 0")
    run, execd, env, said = _Run(), [], {}, []
    slept = []

    def sleep(s):
        slept.append(s)
        lock.unlink()                                         # ... and it finishes
    au.upgrade_before_start(["--once"], "0.6.4", say=said.append, fetch={"webbee": "0.6.5"}.get, wheel=None,
                            which=_which({"pipx": "/bin/pipx"}), run=run, execv=lambda p, a: execd.append(a),
                            env=env, lock_path=lambda: str(lock), sleep=sleep)
    assert run.calls == [] and slept and execd and env[au.DONE_ENV] == "1"   # no second install, the new release
    assert "another webbee is updating" in said[0]


def test_the_lock_is_released_before_the_restart_and_a_broken_install_is_done_again(tmp_path, monkeypatch):
    lock = tmp_path / "webbee-upgrade.lock"
    seen = []
    checks = iter([False, True])                              # the first install left it broken, the second is whole
    monkeypatch.setattr(au, "imports_ok", lambda python, run=None, light=False: next(checks))
    run = _Run()
    au.upgrade_before_start([], "0.6.4", say=lambda m: None, fetch={"webbee": "0.6.5"}.get, wheel=None,
                            which=_which({"pipx": "/bin/pipx"}), run=run, env={}, lock_path=lambda: str(lock),
                            execv=lambda p, a: seen.append(lock.exists()))
    assert seen == [False]                                    # no lock left behind by exec
    assert len([c for c in run.calls if "install" in c]) == 2
    assert not lock.exists()


def test_the_lock_lives_in_webbee_s_own_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))      # Windows: webbee's data folder lives there
    monkeypatch.delenv("WEBBEE_RUNTIME_HOME", raising=False)
    path = au._lock_path()
    assert path.startswith(str(tmp_path / "data")) and path.endswith("upgrade.lock")
    import stat
    if os.name != "nt":             # Windows: the user's own profile folder (its ACL), no POSIX mode bits
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) & 0o077 == 0     # only this user


# ── an install that does not import is never left on disk: the release before it goes back, by the same tool ──
def _checks(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(au, "imports_ok", lambda python, run=None, light=False: next(it))


def test_a_release_that_does_not_import_is_rolled_back_by_the_same_tool(tmp_path, monkeypatch):
    _checks(monkeypatch, [False, False, True])                # twice broken; the old release imports again
    run, said, execd = _Run(), [], []
    did = au.upgrade_before_start([], "0.6.4", say=said.append, fetch={"webbee": "0.6.5"}.get, wheel=None,
                                  which=_which({"pipx": "/bin/pipx"}), run=run, env={},
                                  execv=lambda p, a: execd.append(a), lock_path=lambda: str(tmp_path / "upgrade.lock"))
    installs = [c for c in run.calls if "install" in c and c[-1].startswith("webbee")]
    assert [c[-1] for c in installs] == ["webbee==0.6.5", "webbee==0.6.5", "webbee==0.6.4"]
    assert installs[-1][:-1] == installs[0][:-1]                                      # the same tool, the same way
    assert execd == [] and "webbee 0.6.5" not in did
    assert any("does not start" in s and "put 0.6.4 back" in s for s in said)
    assert not (tmp_path / "upgrade.lock").exists()


def test_an_install_that_broke_off_and_left_nothing_importable_is_rolled_back(tmp_path, monkeypatch):
    _checks(monkeypatch, [False, True])                       # after the failed install: broken; after the way back: whole
    calls, said = [], []

    def run(cmd, **kw):                                       # the new release's install fails; the way back works
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1 if "webbee==0.6.5" in cmd else 0, stdout="", stderr="")
    au.upgrade_before_start([], "0.6.4", say=said.append, fetch={"webbee": "0.6.5", "imperal-mcp": None}.get,
                            wheel=None, which=_which({}), run=run, env={}, execv=lambda *a: 1 / 0,
                            lock_path=lambda: str(tmp_path / "upgrade.lock"))
    assert [c[-1] for c in calls if c[-1].startswith("webbee")] == ["webbee==0.6.5", "webbee==0.6.4"]
    assert any("put 0.6.4 back" in s for s in said)


def test_a_failed_rollback_says_what_to_run(tmp_path, monkeypatch):
    _checks(monkeypatch, [False, False, False])
    said = []
    monkeypatch.setattr(au, "find_uv", lambda which=None: "/bin/uv")
    au.upgrade_before_start([], "0.6.4", say=said.append, fetch={"webbee": "0.6.5"}.get, wheel=None,
                            which=_which({}), run=_Run(), env={}, execv=lambda *a: 1 / 0,
                            lock_path=lambda: str(tmp_path / "upgrade.lock"))
    assert any("could not be put back" in s and "webbee==0.6.4" in s for s in said)


def test_a_light_install_is_checked_and_rolled_back_with_its_old_python(tmp_path):
    light = "/usr/bin/python3.8"
    checks, installs = [], []

    def run(cmd, **kw):
        (checks if "-c" in cmd else installs).append(list(cmd))
        rc = 1 if "-c" in cmd and len(checks) <= 2 else 0     # the new release's launcher does not import, twice
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")
    said, execd = [], []
    au.upgrade_before_start([], "0.6.4", say=said.append, fetch={"webbee": "0.6.5", "imperal-mcp": None}.get,
                            wheel=None, which=_which({}), run=run, env={au.LIGHT_ENV: light},
                            execv=lambda p, a: execd.append(a), lock_path=lambda: str(tmp_path / "upgrade.lock"))
    assert len(checks) == 3 and all(c[0] == light and "webbee._launch" in c[2] for c in checks)
    assert [c for c in installs if c[-1].startswith("webbee")][-1] == [light, "-m", "pip", "install", "--upgrade", "--no-cache-dir", "webbee==0.6.4"]
    assert execd == [] and any("put 0.6.4 back" in s for s in said)
    ok = []                                                    # a whole light install: checked, then the restart

    def run_ok(cmd, **kw):
        ok.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    au.upgrade_before_start([], "0.6.4", say=lambda m: None, fetch={"webbee": "0.6.5"}.get, wheel=None,
                            which=_which({}), run=run_ok, env={au.LIGHT_ENV: light},
                            execv=lambda p, a: execd.append(a), lock_path=lambda: str(tmp_path / "upgrade.lock"))
    assert any(c[0] == light and "-c" in c and "webbee._launch" in c[2] for c in ok) and execd


# ── a lock names its holder: a dead one is taken at once, a live one is waited for a bounded while ──
def _dead_pid() -> int:
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_a_lock_whose_upgrader_died_is_taken_at_once(tmp_path):
    import os
    import time
    lock = tmp_path / "upgrade.lock"
    lock.write_text(f"{_dead_pid()} {time.time():.0f}")       # Ctrl-C a second ago
    slept, run = [], _Run()
    au.upgrade_before_start([], "0.6.4", say=lambda m: None, fetch={"webbee": "0.6.5"}.get, wheel=None,
                            which=_which({"pipx": "/bin/pipx"}), run=run, env={}, execv=lambda p, a: None,
                            lock_path=lambda: str(lock), sleep=slept.append)
    assert slept == [] and any("webbee==0.6.5" in c for c in run.calls)
    lock.write_text(f"{_dead_pid()} {time.time():.0f}")
    assert au._lock(str(lock)) and lock.read_text().split()[0] == str(os.getpid())
    assert au.LOCK_WAIT_S < au.LOCK_STALE_S


def test_a_live_lock_is_waited_for_a_bounded_while_then_the_start_goes_on_without_upgrading(tmp_path, monkeypatch):
    import os
    import time
    lock = tmp_path / "upgrade.lock"
    lock.write_text(f"{os.getpid()} {time.time():.0f}")       # a live upgrader (this very process)
    monkeypatch.setattr(au, "LOCK_WAIT_S", 5)
    slept, run, said, execd = [], _Run(), [], []
    did = au.upgrade_before_start([], "0.6.4", say=said.append, fetch={"webbee": "0.6.5"}.get, wheel=None,
                                  which=_which({"pipx": "/bin/pipx"}), run=run, env={},
                                  execv=lambda p, a: execd.append(a), lock_path=lambda: str(lock), sleep=slept.append)
    assert len(slept) == 5 and run.calls == [] and execd == [] and did == []
    assert any("still updating" in s and "without updating" in s for s in said)
    assert lock.exists()                                      # never removed from under its holder


def test_an_upgrader_that_dies_during_the_wait_is_taken_over(tmp_path):
    import os
    import time
    lock = tmp_path / "upgrade.lock"
    lock.write_text(f"{os.getpid()} {time.time():.0f}")
    dead = _dead_pid()
    slept, run = [], _Run()

    def sleep(s):
        slept.append(s)
        lock.write_text(f"{dead} {time.time():.0f}")         # ... and it is killed midway
    au.upgrade_before_start([], "0.6.4", say=lambda m: None, fetch={"webbee": "0.6.5"}.get, wheel=None,
                            which=_which({"pipx": "/bin/pipx"}), run=run, env={}, execv=lambda p, a: None,
                            lock_path=lambda: str(lock), sleep=sleep)
    assert len(slept) == 1 and any("webbee==0.6.5" in c for c in run.calls)


# ── owner 2026-11-06: always the newest, from git. Live: `pipx reinstall webbee` put back 0.6.7 while 0.7.5 was out --
#    pipx had recorded 0.6.7's wheel address and reinstalls its record; the record is now the stable branch ──
WB, MCP_G, SDK_G = au.git_spec(au.DIST), au.git_spec(au.MCP), au.git_spec(au.SDK)
MCP_W = au.MCP_BESIDE_WEBBEE              # beside webbee: no code extra (it would pull webbee from PyPI)
H_OLD, H_NEW, H_TAG = "a" * 40, "b" * 40, "c" * 40


def _ls_remote(stable_sha):
    lines = [f"{H_OLD}\trefs/tags/v0.7.4",                       # a lightweight tag
             f"{H_TAG}\trefs/tags/v0.7.5", f"{H_NEW}\trefs/tags/v0.7.5^{{}}",   # an annotated one: its commit
             f"{stable_sha}\trefs/heads/stable"]
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines) + "\n", stderr="")


def test_the_newest_release_is_the_tag_the_stable_branch_stands_on():
    assert au.stable_version("webbee", "/bin/git", run=_ls_remote(H_NEW)) == "0.7.5"
    assert au.stable_version("webbee", "/bin/git", run=_ls_remote(H_OLD)) == "0.7.4"
    assert au.stable_version("webbee", "/bin/git", run=_ls_remote("d" * 40)) is None   # between tags: not a release
    assert au.stable_version("webbee", None) is None                                    # no git
    failed = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, stdout="", stderr="")   # noqa: E731
    assert au.stable_version("webbee", "/bin/git", run=failed) is None                  # GitHub out of reach
    seen = []
    au.stable_version("imperal-sdk", "/bin/git", run=lambda cmd, **kw: seen.append((cmd, kw)) or 1 / 0)
    assert seen[0][0][:3] == ["/bin/git", "ls-remote", au.REPOS["imperal-sdk"]]
    assert seen[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"                              # never waits on a prompt


def test_the_spec_is_the_branch_never_a_version():
    assert WB == "webbee @ git+https://github.com/imperalcloud/webbee-code.git@stable"
    assert MCP_G == "imperal-mcp[code] @ git+https://github.com/imperalcloud/imperal-mcp.git@stable"
    assert SDK_G == "imperal-sdk[universal-index] @ git+https://github.com/imperalcloud/imperal-sdk.git@stable"
    assert au.MCP_GIT_SPEC == MCP_G
    assert MCP_W == "imperal-mcp @ git+https://github.com/imperalcloud/imperal-mcp.git@stable"


def _pipx_venv(tmp_path, main, injected=()):
    import json
    venv = tmp_path / "pipx" / "venvs" / "webbee"
    venv.mkdir(parents=True)
    (venv / "pipx_metadata.json").write_text(json.dumps({
        "main_package": {"package_or_url": main},
        "injected_packages": {s.split()[0]: {"package_or_url": s} for s in injected}}))
    return str(venv)


def test_what_pipx_and_uv_recorded_is_read(tmp_path):
    pinned = _pipx_venv(tmp_path / "a", "webbee @ https://files.pythonhosted.org/x/webbee-0.6.7-py3-none-any.whl")
    assert not au.on_stable("pipx", pinned, (WB,))
    good = _pipx_venv(tmp_path / "b", WB, (MCP_W, SDK_G))
    assert au.on_stable("pipx", good, au.WEBBEE_ENV_SPECS)
    coded = _pipx_venv(tmp_path / "d", WB, (MCP_G, SDK_G))                   # mcp WITH its code extra beside webbee
    assert not au.on_stable("pipx", coded, au.WEBBEE_ENV_SPECS)
    half = _pipx_venv(tmp_path / "c", WB)                                     # its packages still from PyPI
    assert not au.on_stable("pipx", half, au.WEBBEE_ENV_SPECS)
    uv = tmp_path / "uv" / "tools" / "imperal-mcp"
    uv.mkdir(parents=True)
    (uv / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "imperal-mcp", extras = ["code"], '
                                        'specifier = "==0.9.5" }]\n')
    assert not au.on_stable("uv", str(uv), (MCP_G,))
    (uv / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [\n'
        '    { name = "imperal-mcp", extras = ["code"], git = "https://github.com/imperalcloud/imperal-mcp.git?rev=stable" },\n'
        '    { name = "imperal-sdk", extras = ["universal-index"], git = "https://github.com/imperalcloud/imperal-sdk.git?rev=stable" },\n'
        '    { name = "webbee", git = "https://github.com/imperalcloud/webbee-code.git?rev=stable" },\n]\n')
    assert au.on_stable("uv", str(uv), au.MCP_ENV_SPECS)
    assert not au.on_stable("pipx", str(tmp_path / "missing"), (WB,))   # unreadable: moved, never assumed
    assert au.on_stable("pip", "/venv", (WB,))                          # plain pip keeps no record


def test_from_git_each_tool_installs_and_records_the_branch():
    uv = au.self_command("0.7.5", prefix="/u/.local/share/uv/tools/webbee", env={}, uv="/bin/uv", source="git")
    assert uv == ["/bin/uv", "tool", "install", "--force", "--refresh", "--with", MCP_W, "--with", SDK_G, WB]
    px = au.self_command("0.7.5", prefix="/h/.local/pipx/venvs/webbee", env={}, pipx="/bin/pipx", source="git")
    assert px == [["/bin/pipx", "install", "--force", WB], ["/bin/pipx", "inject", "--force", "webbee", MCP_W, SDK_G]]
    kept = au.self_command("0.7.5", prefix="/h/.local/pipx/venvs/webbee", env={}, pipx="/bin/pipx", source="git",
                           record=False)                                    # already on the branch: its own record
    assert kept == ["/bin/pipx", "upgrade", "--include-injected", "webbee"]
    pip = au.self_command("0.7.5", prefix="/v", executable="/v/bin/python", env={}, source="git")
    assert pip == ["/v/bin/python", "-m", "pip", "install", "--upgrade", "--no-cache-dir", WB, MCP_W, SDK_G]
    light = au.self_command("0.7.5", env={au.LIGHT_ENV: "/usr/bin/python3.7"}, source="git", wheel=None)
    assert light[-1] == "webbee==0.7.5"                                     # 3.7 builds nothing from source: PyPI


def test_the_same_release_recorded_by_a_version_is_moved_onto_the_branch(tmp_path):
    venv = _pipx_venv(tmp_path, "webbee @ https://files.pythonhosted.org/x/webbee-0.7.5-py3-none-any.whl")
    run, execd, env, said = _Run(), [], {}, []
    stable = {"webbee": "0.7.5", "imperal-mcp": "0.9.5"}.get
    au.upgrade_before_start(["--once"], "0.7.5", say=said.append, fetch=lambda d: 1 / 0, wheel=None,
                            which=_which({"pipx": "/bin/pipx", "git": "/bin/git"}), run=run, env=env,
                            execv=lambda p, a: execd.append(a), lock_path=lambda: str(tmp_path / "upgrade.lock"),
                            stable=lambda d, git: stable(d), prefix=venv)
    assert ["/bin/pipx", "install", "--force", WB] in run.calls
    assert ["/bin/pipx", "inject", "--force", "webbee", MCP_W, SDK_G] in run.calls
    assert execd and any("onto its release branch" in s for s in said)


def test_on_the_branch_and_newest_nothing_is_installed(tmp_path, monkeypatch):
    venv = _pipx_venv(tmp_path, WB, (MCP_W, SDK_G))
    exe = tmp_path / "pipx" / "venvs" / "imperal-mcp" / "bin" / "imperal-mcp"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    _pipx_venv(tmp_path / "m", MCP_G, (SDK_G, WB))
    import shutil
    shutil.copy(tmp_path / "m" / "pipx" / "venvs" / "webbee" / "pipx_metadata.json", exe.parent.parent)
    monkeypatch.setattr(au, "find_uv", lambda which=None: None)
    run = _Run(stdout="0.9.5 True")
    au.upgrade_before_start([], "0.7.5", say=lambda m: None, fetch=lambda d: 1 / 0, wheel=None,
                            which=_which({"pipx": "/bin/pipx", "git": "/bin/git", "imperal-mcp": str(exe)}), run=run,
                            env={}, execv=lambda *a: 1 / 0, lock_path=lambda: str(tmp_path / "upgrade.lock"),
                            stable=lambda d, git: {"webbee": "0.7.5", "imperal-mcp": "0.9.5"}[d], prefix=venv)
    assert not any("install" in c or "upgrade" in c or "inject" in c for c in run.calls)


def test_a_newer_release_on_the_branch_is_taken_by_the_install_s_own_record(tmp_path):
    venv = _pipx_venv(tmp_path, WB, (MCP_W, SDK_G))
    run, execd = _Run(), []
    au.upgrade_before_start([], "0.7.4", say=lambda m: None, fetch=lambda d: 1 / 0, wheel=None,
                            which=_which({"pipx": "/bin/pipx", "git": "/bin/git"}), run=run, env={},
                            execv=lambda p, a: execd.append(a), lock_path=lambda: str(tmp_path / "upgrade.lock"),
                            stable=lambda d, git: "0.7.5", prefix=venv)
    assert run.calls[0] == ["/bin/pipx", "upgrade", "--include-injected", "webbee"] and execd


def test_no_git_or_github_out_of_reach_falls_back_to_pypi(tmp_path):
    run = _Run()
    au.upgrade_before_start([], "0.7.4", say=lambda m: None, fetch={"webbee": "0.7.5"}.get, wheel=None,
                            which=_which({"pipx": "/bin/pipx", "git": "/bin/git"}), run=run, env={},
                            execv=lambda p, a: None, lock_path=lambda: str(tmp_path / "upgrade.lock"),
                            stable=lambda d, git: None, prefix="/h/.local/pipx/venvs/webbee")
    assert run.calls[0][-1] == "webbee==0.7.5"


def test_the_shell_s_imperal_mcp_recorded_by_a_version_is_moved_onto_the_branch(tmp_path):
    uv = tmp_path / "uv" / "tools" / "imperal-mcp"
    uv.mkdir(parents=True)
    (uv / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "imperal-mcp", extras = ["code"], '
                                        'specifier = "==0.9.5" }]\n')
    inst = {"tool": "uv", "python": str(uv / "bin" / "python"), "prefix": str(uv), "version": "0.9.5", "code": True}
    assert au.mcp_command(inst, "0.9.5", "/bin/uv", None) is None               # PyPI: nothing to do
    assert au.mcp_command(inst, "0.9.5", "/bin/uv", None, source="git") == [
        "/bin/uv", "tool", "install", "--force", "--refresh", "--with", SDK_G, "--with", WB, MCP_G]
    assert au.mcp_command(dict(inst, version="0.9.6"), "0.9.5", "/bin/uv", None, source="git") is None  # own build
    assert au.mcp_command({}, "0.9.5", None, "/bin/pipx", source="git") == [
        ["/bin/pipx", "install", "--force", MCP_G], ["/bin/pipx", "inject", "--force", "imperal-mcp", SDK_G, WB]]


def test_the_steps_of_one_install_stop_at_the_first_that_fails():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
    assert au._run([["/bin/pipx", "install", "x"], ["/bin/pipx", "inject", "y"]], run) is False
    assert len(calls) == 1
    shown = au._show([["/bin/pipx", "install", "--force", WB], ["/bin/pipx", "inject", "--force", "webbee", MCP_G]])
    assert shown == f"/bin/pipx install --force '{WB}' && /bin/pipx inject --force webbee '{MCP_G}'"


def test_a_git_release_that_does_not_import_is_put_back_by_its_own_tag(tmp_path, monkeypatch):
    _checks(monkeypatch, [False, False, True])
    venv = _pipx_venv(tmp_path, WB, (MCP_W, SDK_G))
    run, said = _Run(), []
    au.upgrade_before_start([], "0.7.4", say=said.append, fetch=lambda d: 1 / 0, wheel=None,
                            which=_which({"pipx": "/bin/pipx", "git": "/bin/git"}), run=run, env={},
                            execv=lambda *a: 1 / 0, lock_path=lambda: str(tmp_path / "upgrade.lock"),
                            stable=lambda d, git: "0.7.5", prefix=venv)
    assert ["/bin/pipx", "install", "--force", au.git_spec("webbee", "v0.7.4")] in run.calls
    assert any("put 0.7.4 back" in s for s in said)


def test_the_webbee_beside_imperal_mcp_follows_webbee_s_releases(tmp_path):
    uv = tmp_path / "uv" / "tools" / "imperal-mcp"
    uv.mkdir(parents=True)
    (uv / "uv-receipt.toml").write_text("".join(
        f'{{ name = "x", git = "{u}?rev=stable" }}\n' for u in au.REPOS.values()))
    inst = {"tool": "uv", "python": str(uv / "bin" / "python"), "prefix": str(uv), "version": "0.9.5", "code": True,
            "webbee": "0.7.6"}
    assert au.mcp_command(inst, "0.9.5", "/bin/uv", None, source="git", webbee_newest="0.7.6") is None
    assert au.mcp_command(inst, "0.9.5", "/bin/uv", None, source="git", webbee_newest="0.7.7") == [
        "/bin/uv", "tool", "install", "--force", "--refresh", "--with", SDK_G, "--with", WB, MCP_G]
    run = _Run(stdout="0.9.5 True 0.7.6")
    exe = uv / "bin" / "imperal-mcp"
    exe.parent.mkdir()
    exe.write_text("")
    got = au.mcp_installed(which=lambda n: str(exe), run=run)
    assert got["webbee"] == "0.7.6" and got["code"] is True


# live 2026-11-14 (Windows): the self-upgrade of a pip / pipx install failed on every start -- the running webbee.exe
# cannot be overwritten there -- and "v... available" stayed for good

def test_a_running_launcher_is_moved_aside_on_windows_and_put_back_if_the_install_failed(tmp_path):
    (tmp_path / "webbee.exe").write_text("old launcher")
    moved = au.free_launchers(dirs=[str(tmp_path)], windows=True)
    assert [m[0] for m in moved] == [str(tmp_path / "webbee.exe")] and not (tmp_path / "webbee.exe").exists()
    au.put_back(moved)                                     # the installer wrote none: the old one is back
    assert (tmp_path / "webbee.exe").read_text() == "old launcher"
    moved = au.free_launchers(dirs=[str(tmp_path)], windows=True)
    (tmp_path / "webbee.exe").write_text("new launcher")   # the installer wrote the new one
    au.put_back(moved)
    assert (tmp_path / "webbee.exe").read_text() == "new launcher"
    assert au.sweep_aside(dirs=[str(tmp_path)]) == 1 and sorted(p.name for p in tmp_path.iterdir()) == ["webbee.exe"]


def test_elsewhere_no_launcher_is_moved(tmp_path):
    (tmp_path / "webbee.exe").write_text("x")
    assert au.free_launchers(dirs=[str(tmp_path)], windows=False) == [] and (tmp_path / "webbee.exe").exists()


def test_the_windows_self_upgrade_frees_the_launcher_before_installing(tmp_path, monkeypatch):
    (tmp_path / "webbee.exe").write_text("old")
    monkeypatch.setattr(au, "_WINDOWS", True)
    monkeypatch.setattr(au, "_script_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(au, "find_uv", lambda which=None: "/bin/uv")
    seen = []

    class _R(_Run):
        def __call__(self, cmd, **kw):
            if "install" in cmd:
                seen.append(sorted(p.name for p in tmp_path.iterdir()))
            return super().__call__(cmd, **kw)
    said = []
    au.upgrade_before_start([], "0.5.14", wheel=lambda d, v: None, say=said.append,
                            fetch={"webbee": "0.6.0", "imperal-mcp": None}.get, which=_which({}), run=_R(rc=1),
                            execv=lambda *a: 1 / 0, env={}, stable=lambda d, g: None, lock_path=None)
    assert seen and all(f"webbee.exe{au.ASIDE}" in n for n in seen[0])     # moved aside while the installer ran
    assert (tmp_path / "webbee.exe").read_text() == "old"                    # failed: put back


def test_on_windows_the_new_release_is_run_and_its_exit_code_passed_on(monkeypatch):
    monkeypatch.setattr(au, "_WINDOWS", True)
    monkeypatch.setattr(au, "find_uv", lambda which=None: "/bin/uv")
    monkeypatch.setattr(au, "imports_ok", lambda python, run=None, light=False: True)
    monkeypatch.setattr(au, "free_launchers", lambda: [])
    started = []
    monkeypatch.setattr(au.subprocess, "call", lambda cmd: started.append(cmd) or 7)
    with pytest.raises(SystemExit) as ex:
        au.upgrade_before_start(["--mode", "plan"], "0.5.14", say=lambda m: None, fetch={"webbee": "0.6.0"}.get,
                                wheel=None, which=_which({}), run=_Run(), execv=lambda *a: 1 / 0, env={},
                                stable=lambda d, g: None, lock_path=None)
    assert ex.value.code == 7 and started and started[0][-2:] == ["--mode", "plan"]
