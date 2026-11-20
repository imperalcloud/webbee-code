"""Run the field gate against the INSTALLED imperal-mcp and webbee.

    python -m ci.field_gate.run [--account] [--shell mcp|webbee|both] [--only ID ...] [--json out.json]

Prints PASS/FAIL per scenario and shell in os_check's style; exits 1 on any FAIL. A fresh fixture and a fresh HOME per
shell: nothing of the machine's caches, credentials or spool takes part. --account runs the scenarios that need a
logged-in test user (IMPERAL_GATE_HOME: the HOME holding that user's credentials).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from ci.field_gate import scenarios_code, scenarios_contract  # noqa: F401  (registration)
from ci.field_gate.drivers import McpDriver, WebbeeDriver
from ci.field_gate.fixture import build_fixture
from ci.field_gate.registry import SCENARIOS, Ctx, Outcome

try:
    from ci.field_gate import scenarios_account, scenarios_long, scenarios_wait  # noqa: F401
except ImportError:
    pass


def _selected(shell: str, account: bool, only: list[str]):
    picked = [s for s in SCENARIOS if shell in s.shells and (account or not s.account) and (not only or s.id in only)]
    return [s for s in picked if not s.mutates] + [s for s in picked if s.mutates]


async def _run_shell(shell: str, account: bool, only: list[str], work: Path) -> list[dict]:
    fx = build_fixture(work / f"ws-{shell}")
    home = work / f"home-{shell}"
    login = _gate_credentials() if account else None
    if login:                                   # the test user's login in a fresh HOME; the refreshed token goes back
        (home / ".imperal").mkdir(parents=True, exist_ok=True)
        shutil.copy2(login, home / ".imperal" / "credentials.json")
    rows = []
    if shell == "mcp":
        driver = McpDriver(fx.root, home)
    else:
        driver = WebbeeDriver(fx.root, home)
    try:
        async with driver as d:
            ctx = Ctx(fixture=fx, shell=shell, mcp=d if shell == "mcp" else None,
                      webbee=d if shell == "webbee" else None, home=home)
            for s in _selected(shell, account, only):
                try:
                    out = await s.run(ctx)
                except Exception as exc:
                    out = Outcome(False, "an answer",
                                  f"{type(exc).__name__}: {exc} | {traceback.format_exc(limit=2)[-300:]}")
                finally:
                    _login_back(home, login)
                rows.append({"id": s.id, "items": list(s.items), "shell": shell, "ok": out.ok,
                             "expected": out.expected, "got": " ".join(str(out.got).split())[:1500]})
                print(("PASS " if out.ok else "FAIL ")
                      + f"{s.id} [{shell}] -- expected {out.expected}; got {rows[-1]['got'][:300]}", flush=True)
    finally:
        _login_back(home, login)
    return rows


def _login_back(home: Path, login: Path | None) -> None:
    """The login the run refreshed goes back to the gate's HOME -- after EVERY scenario and however the run ends.
    A refresh replaces the token and the old one stops working, so a run that ended without returning it left the
    test user logged out (2026-11-20: the gate was stopped mid-run, the next run's eight scenarios each died in 4 s
    on "session expired")."""
    fresh = home / ".imperal" / "credentials.json"
    if login is None or not fresh.exists():
        return
    try:
        if fresh.read_bytes() != login.read_bytes():
            tmp = login.with_name(login.name + ".gate-tmp")
            shutil.copy2(fresh, tmp)
            os.replace(tmp, login)
    except OSError as exc:
        print(f"the refreshed login did not go back to {login}: {exc}", file=sys.stderr, flush=True)


def _gate_credentials() -> Path | None:
    """IMPERAL_GATE_HOME: the HOME where the test user logged in once (`HOME=<it> imperal-mcp login`)."""
    gate = os.environ.get("IMPERAL_GATE_HOME")
    path = Path(gate) / ".imperal" / "credentials.json" if gate else None
    if path is None or not path.exists():
        raise SystemExit("--account needs IMPERAL_GATE_HOME holding the test user's .imperal/credentials.json")
    return path


async def main_async(args) -> int:
    shells = ["mcp", "webbee"] if args.shell == "both" else [args.shell]
    work = Path(tempfile.mkdtemp(prefix="field-gate-"))
    rows = []
    for shell in shells:
        rows += await _run_shell(shell, args.account, args.only or [], work)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    failed = [r for r in rows if not r["ok"]]
    print(f"\n{len(rows) - len(failed)} passed, {len(failed)} failed", flush=True)
    return 1 if failed else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--account", action="store_true")
    p.add_argument("--shell", default="both", choices=["mcp", "webbee", "both"])
    p.add_argument("--only", nargs="*")
    p.add_argument("--json")
    # a stop from outside (kill, a closed terminal) ends the run the way Ctrl-C does: the finally blocks run and
    # the refreshed login goes back
    import signal
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.default_int_handler)
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
