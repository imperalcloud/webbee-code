"""Owner 2026-10-25: webbee starts on the newest release of itself and of the shell's imperal-mcp (with its code
extra), on any system -- the upgrade first, then the start."""
import subprocess

from webbee import autoupdate as au


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
    lock.write_text("1 0")                                    # another webbee is upgrading right now
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
    monkeypatch.setattr(au, "imports_ok", lambda python, run=None: next(checks))
    run = _Run()
    au.upgrade_before_start([], "0.6.4", say=lambda m: None, fetch={"webbee": "0.6.5"}.get, wheel=None,
                            which=_which({"pipx": "/bin/pipx"}), run=run, env={}, lock_path=lambda: str(lock),
                            execv=lambda p, a: seen.append(lock.exists()))
    assert seen == [False]                                    # no lock left behind by exec
    assert len([c for c in run.calls if "install" in c]) == 2
    assert not lock.exists()
