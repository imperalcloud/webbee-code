"""Real-system checks the unit tests cannot make (run by .github/workflows/os-check.yml on Windows, macOS, Linux).

Owner 2026-11-14: "they install a version and webbee says a newer one is out, though they run the newest" (Windows,
Python 3.11) -- each check below runs the real thing on the real system and fails loudly.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time

FAILED = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)


def py(*code_lines, env=None):
    return subprocess.run([sys.executable, "-c", "\n".join(code_lines)], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def main():
    import webbee
    from webbee import update

    # 1. the console script runs and names this release
    exe = "webbee.exe" if os.name == "nt" else "webbee"
    scripts = os.path.dirname(sys.executable) if os.name == "nt" else os.path.dirname(sys.executable)
    cand = [os.path.join(scripts, exe), os.path.join(scripts, "Scripts", exe)]
    found = next((c for c in cand if os.path.isfile(c)), None)
    got = subprocess.run([found or "webbee", "--version"], capture_output=True, text=True,
                         env={**os.environ, "WEBBEE_NO_AUTOUPDATE": "1"})
    check("console script prints this release", got.returncode == 0 and webbee.__version__ in got.stdout,
          (got.stdout + got.stderr).strip()[:200])

    # 2. a left-over older dist-info ahead on sys.path (the Windows user-site case)
    stray = tempfile.mkdtemp()
    d = os.path.join(stray, "webbee-0.3.49.dist-info")
    os.makedirs(d)
    with open(os.path.join(d, "METADATA"), "w") as f:
        f.write("Metadata-Version: 2.1\nName: webbee\nVersion: 0.3.49\n")
    r = py("import importlib.metadata as m, webbee.update as u, webbee",
           "print(m.version('webbee')); print(u.installed_version()); print(webbee.__version__)",
           "print(repr(u.restart_notice(webbee.__version__, u.installed_version())))", env={"PYTHONPATH": stray})
    lines = r.stdout.split("\n")
    check("the stray dist-info is what the metadata names (the field case reproduced)",
          r.returncode == 0 and lines[0] == "0.3.49", r.stdout + r.stderr)
    check("installed_version reads the running package, never the stray dist-info",
          r.returncode == 0 and lines[1] == webbee.__version__ and lines[3] == "''", r.stdout + r.stderr)

    # 3. the upgrade command is this install's own
    hint = update.upgrade_hint()
    check("upgrade hint for a pip install names this interpreter", "-m pip install -U webbee" in hint
          and sys.executable in hint, hint)

    # 4. a version with a suffix is read by its numbers
    check("a suffixed version is not older than everything",
          update.check_for_update(webbee.__version__ + "+local", cache_path=os.path.join(stray, "u.json"), now=1.0,
                                  fetch=lambda: webbee.__version__) is None)

    # 5. Windows: a RUNNING console-script .exe -- reinstalled with it moved aside
    if os.name == "nt":
        windows_launcher_swap()

    print(f"\n{len(FAILED)} failed: {FAILED}" if FAILED else "\nall checks passed")
    return 1 if FAILED else 0


def windows_launcher_swap():
    from webbee import autoupdate as au
    pkg = tempfile.mkdtemp()
    os.makedirs(os.path.join(pkg, "sleeper"))
    with open(os.path.join(pkg, "sleeper", "__init__.py"), "w") as f:
        f.write("import time\n\ndef main():\n    time.sleep(120)\n")
    with open(os.path.join(pkg, "pyproject.toml"), "w") as f:
        f.write(textwrap.dedent("""\
            [project]
            name = "sleeper"
            version = "1.0"
            [project.scripts]
            sleeper = "sleeper:main"
            [build-system]
            requires = ["setuptools>=61"]
            build-backend = "setuptools.build_meta"
            [tool.setuptools]
            packages = ["sleeper"]
            """))
    pip = [sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", "--disable-pip-version-check",
           "-q", pkg]
    subprocess.run(pip, check=True)
    scripts = os.path.join(sys.prefix, "Scripts")
    exe = os.path.join(scripts, "sleeper.exe")
    check("the console-script launcher exists", os.path.isfile(exe), exe)
    proc = subprocess.Popen([exe])
    time.sleep(3)
    try:
        bare = subprocess.run(pip, capture_output=True, text=True)
        print(f"INFO reinstall with the launcher running and NOT moved: exit {bare.returncode} "
              f"{(bare.stdout + bare.stderr).strip()[-300:]}", flush=True)
        moved = au.free_launchers(names=("sleeper",), dirs=[scripts], windows=True)
        check("the running launcher is moved aside", len(moved) == 1 and os.path.isfile(moved[0][1]), str(moved))
        swap = subprocess.run(pip, capture_output=True, text=True)
        check("reinstall succeeds with the running launcher moved aside", swap.returncode == 0,
              (swap.stdout + swap.stderr).strip()[-300:])
        check("a new launcher is in place", os.path.isfile(exe))
        au.put_back(moved)               # the installer wrote one: nothing is put back over it
        check("put_back leaves the new launcher", os.path.isfile(exe) and os.path.isfile(moved[0][1]))
    finally:
        proc.kill()
        proc.wait()
    time.sleep(1)
    n = au.sweep_aside(dirs=[scripts])
    check("the launcher moved aside is removed once it no longer runs", n >= 1, f"removed {n}")


if __name__ == "__main__":
    sys.exit(main())
