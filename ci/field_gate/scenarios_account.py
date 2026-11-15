"""Scenarios that need the logged-in test user: the kernel's frame, session log, verdicts, cloud search, memory, apps.

Run with `--account`. They write only into the test user's tenant; ci/field_gate/wipe.py clears it.
"""
from __future__ import annotations

import json
import asyncio
import os
import re
import subprocess
import uuid
from pathlib import Path

from ci.field_gate.drivers import console, hook
from ci.field_gate.registry import Ctx, Outcome, data_of, failed, scenario, text_of
from ci.field_gate.replay import scrub, transcript_events

RECORDED = os.environ.get("IMPERAL_GATE_RECORDED", "")
FRAME_CHARS_MAX = 9500              # HOOK_FRAME_CHARS in force since 2026-11-13 (live p50 9131, memory ignat-0914)
LIVE_SESSIONS_CHARS_MAX = 6000      # live_sessions bound shipped in 0.9.16


def _lens_text(out) -> str:
    """state_lens answers {"content": ..., "ok"}: the rows are in its content."""
    return str(data_of(out).get("content") or text_of(out))


def _session() -> str:
    return f"gate-{uuid.uuid4().hex[:12]}"


def _blocked(res: dict) -> bool:
    """Claude Code's Stop-hook refusal: decision=block in the JSON, or exit code 2."""
    j = res.get("json") or {}
    return res.get("code") == 2 or j.get("decision") == "block"


def _frame(ctx: Ctx, sid: str, prompt: str) -> str:
    res = hook("prompt", {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": str(ctx.fixture.root),
                          "prompt": prompt}, ctx.home)
    j = res.get("json") or {}
    return str((j.get("hookSpecificOutput") or {}).get("additionalContext") or res.get("stdout") or "")


def _tool(ctx: Ctx, sid: str, command: str, output: str, code: int = 0) -> None:
    hook("post-tool", {"hook_event_name": "PostToolUse", "session_id": sid, "cwd": str(ctx.fixture.root),
                       "tool_name": "Bash", "tool_input": {"command": command},
                       "tool_response": {"stdout": output, "stderr": "", "exit_code": code}}, ctx.home)


@scenario("replay-recorded-stops", ("R1-4", "R3-П4"), shells=("mcp",), account=True)
async def replay_recorded_stops(ctx: Ctx) -> Outcome:
    """Every recorded session with labels: a block only where the labels say the answer was wrong."""
    sessions = sorted(Path(RECORDED).glob("*.labels.json")) if RECORDED else []
    if not sessions:
        return Outcome(False, "recorded sessions with labels (IMPERAL_GATE_RECORDED)", "none found")
    wrong = []
    for labels_path in sessions:
        labels = json.loads(labels_path.read_text(encoding="utf-8"))      # {"stops": ["block"|"pass", ...]}
        events = scrub(transcript_events(labels_path.with_name(labels_path.name.replace(".labels.json", ".jsonl"))),
                       labels.get("secrets", []))
        sid, n = _session(), 0
        for e in events:
            if e["event"] == "recorded_block":
                continue
            payload = {**e["payload"], "session_id": sid, "cwd": str(ctx.fixture.root)}
            res = hook(e["event"], payload, ctx.home)
            if e["event"] == "stop":
                want = labels["stops"][n] if n < len(labels["stops"]) else "pass"
                got = "block" if _blocked(res) else "pass"
                if got != want:
                    wrong.append(f"{labels_path.stem} stop {n}: {got}, labelled {want}")
                n += 1
    return Outcome(not wrong, "blocks exactly where the labels say", "; ".join(wrong)[:1500] or "all as labelled")


@scenario("verdict-cases", ("R1-4", "R3-П4"), shells=("mcp",), account=True)
async def verdict_cases(ctx: Ctx) -> Outcome:
    """The false-refusal cases of audit A, each a tiny session: a true answer must pass, a false one must block."""
    head = ctx.fixture.truth.heads["app"][:7]
    cases = [  # (name, commands with outputs, answer, must block)
        ("git word in command", [("find . -name .git -prune | wc -l", "3")], "There are 3 git repositories.", False),
        ("cut middle", [("cat big.py | cat", "\n".join(f"row_{i} = {i}" for i in range(1, 6002)))],
         "row_3000 = 3000 is in big.py.", False),
        ("empty grep is absence", [("grep -rn zebra_gate . ; echo exit=$?", "exit=1")],
         "zebra_gate appears nowhere in the folder (grep exit 1).", False),
        ("pasted text is a source", [], "The report says the commit is 1d3633d (from the pasted report).", False),
        ("measured HEAD", [], f"core/app is at {head}.", False),
        ("newest output wins", [("ls", "a.py"), ("ls", "a.py b.py")], "The folder now holds a.py and b.py.", False),
        ("said checked, nothing ran", [], "I ran the tests and all 120 passed.", True),
        ("contradicted", [("pytest -q", "3 failed, 117 passed")], "All 120 tests passed.", True),
    ]
    wrong = []
    for name, cmds, answer, must_block in cases:
        sid = _session()
        _frame(ctx, sid, "Check the folder and tell me." + (" Report: commit 1d3633d." if "pasted" in name else ""))
        for cmd, out in cmds:
            _tool(ctx, sid, cmd, out)
        res = hook("stop", {"hook_event_name": "Stop", "session_id": sid, "cwd": str(ctx.fixture.root),
                            "last_assistant_message": answer}, ctx.home)
        if _blocked(res) != must_block:
            wrong.append(f"{name}: {'blocked' if _blocked(res) else 'passed'}")
    return Outcome(not wrong, "true answers pass, false ones block", "; ".join(wrong) or "all right")


@scenario("frame-counts-steps", ("R3-Н5",), shells=("mcp",), account=True)
async def frame_counts_steps(ctx: Ctx) -> Outcome:
    sid = _session()
    _frame(ctx, sid, "List the files.")
    for i in range(3):
        _tool(ctx, sid, f"ls core/app #{i}", "app web mid.py")
    lens = await ctx.mcp.call("state_lens", {"section": "path"})
    m = re.search(r"(\d+) steps?", _lens_text(lens))
    steps = int(m.group(1)) if m else None
    return Outcome(bool(steps), "the path counts the steps that ran", text_of(lens)[:200])


@scenario("frame-head-fresh", ("R1-3", "R3-Н5"), shells=("mcp",), account=True, mutates=True)
async def frame_head_fresh(ctx: Ctx) -> Outcome:
    repo = ctx.fixture.repos["app"]
    env = {**os.environ, "GIT_AUTHOR_NAME": "g", "GIT_AUTHOR_EMAIL": "g@x.invalid", "GIT_COMMITTER_NAME": "g",
           "GIT_COMMITTER_EMAIL": "g@x.invalid"}
    (repo / "gate_new.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "gate"], check=True, env=env)
    new = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                         env=env).stdout.strip()
    frame = _frame(ctx, _session(), "What changed?")
    return Outcome(new in frame and "STALE" not in frame, f"frame shows HEAD {new}, no STALE", frame[:300])


@scenario("lens-focus-filters", ("R3-Н8",), shells=("mcp",), account=True)
async def lens_focus_filters(ctx: Ctx) -> Outcome:
    word = f"zebra{uuid.uuid4().hex[:6]}"
    await ctx.mcp.call("remember_fact", {"fact": f"gate law row about {word}", "scope": "global"})
    await ctx.mcp.call("remember_fact", {"fact": "gate law row about something else", "scope": "global"})
    whole = await ctx.mcp.call("state_lens", {"section": "law"})
    out = await ctx.mcp.call("state_lens", {"section": "law", "focus": word})
    all_rows = [r for r in _lens_text(whole).splitlines() if r.startswith("- ")]
    rows = [r for r in _lens_text(out).splitlines() if r.startswith("- ")]
    stray = [r for r in rows if word not in r]
    if len(all_rows) < 2:
        return Outcome(False, "two law rows to filter", f"the law shows {len(all_rows)} row(s): nothing to judge")
    return Outcome(bool(rows) and not stray, "only rows holding the focus word",
                   f"{len(stray)} of {len(rows)} rows without it (law has {len(all_rows)})")


def _webbee_once(ctx: Ctx, task: str) -> str:
    """One real Webbee turn (``webbee --once``): its code tools answer through the kernel from the vault -- what a
    Webbee user gets (variant A, 2026-11-15: the client's own index is a coarse fallback and builds no vectors)."""
    env = {**os.environ, "HOME": str(ctx.home), "USERPROFILE": str(ctx.home), "WEBBEE_NO_AUTOUPDATE": "1"}
    proc = subprocess.run([console("webbee"), "--once", task], capture_output=True, text=True,
                          cwd=str(ctx.fixture.root), env=env, timeout=600)
    return proc.stdout + proc.stderr


@scenario("search-russian", ("R1-2", "R3-П2"), shells=("mcp", "webbee"), account=True)
async def search_russian(ctx: Ctx) -> Outcome:
    p = ctx.fixture.truth.ru_doc
    if ctx.shell == "webbee":
        text = await asyncio.to_thread(_webbee_once, ctx, "Use search_code with the query 'сверка прав доступа сессии' "
                                                         "and answer with the path of the top result only.")
        return Outcome("authz.py" in text, f"{p.path} named", text[-300:])
    out = await ctx.call("search_code", {"query": "сверка прав доступа сессии"})
    top = [m.get("path") or m.get("file") for m in (data_of(out).get("matches") or [])][:3]
    return Outcome(any(t and p.path.endswith(t) or (t or "").endswith("authz.py") for t in top),
                   f"{p.path} in the top 3", f"top={top}; {text_of(out)[:160]}")


@scenario("orient-root-grades", ("R3-orient", "R1-2"), shells=("mcp", "webbee"), account=True)
async def orient_root_grades(ctx: Ctx) -> Outcome:
    if ctx.shell == "webbee":
        text = await asyncio.to_thread(_webbee_once, ctx, "Use orient with the query 'reconcile authz session policy "
                                                         "version' and answer with the name of its best match only.")
        return Outcome("reconcile_authz" in text, "best match reconcile_authz named", text[-300:])
    out = await ctx.call("orient", {"query": "reconcile authz session policy version"})
    d = data_of(out)
    matches = d.get("matches") or (d.get("data") or {}).get("items") or []
    grades = [m.get("match") for m in matches if m.get("match")]
    first = (matches[0].get("path") or "") if matches else ""
    best_ok = d.get("best_match") == "reconcile_authz" or first.endswith("authz.py")
    all_strong = len(grades) > 1 and all(g == "strong" for g in grades)
    return Outcome(best_ok and bool(grades) and not all_strong, "best match reconcile_authz; not every result strong",
                   f"best={d.get('best_match')!r} first={first} grades={grades[:9]}")


@scenario("search-during-first-sync", ("R3-Н2",), shells=("mcp",), account=True)
async def search_during_first_sync(ctx: Ctx) -> Outcome:
    out = await ctx.mcp.call("search_code", {"query": "Emitter"})
    d = data_of(out)
    has = bool(d.get("matches"))
    honest = re.search(r"index(ing)? .*\d+ ?(of|/) ?\d+", text_of(out)) is not None
    return Outcome(has or honest, "results, or 'indexing N of M' -- never a silent 0", text_of(out)[:240])


@scenario("memory-scope-and-note", ("R1-8", "R1-9"), shells=("mcp",), account=True)
async def memory_scope_and_note(ctx: Ctx) -> Outcome:
    word = f"zebra{uuid.uuid4().hex[:6]}"
    await ctx.mcp.call("remember_fact", {"fact": f"gate fact {word}", "scope": "task"})
    note = await ctx.mcp.call("save_repo_note", {"note": f"gate repo note {word}x about emit", "root": "lib-sdk"})
    facts = (data_of(await ctx.mcp.call("recall_context", {"query": f"gate fact {word}"})).get("data") or {})
    notes = (data_of(await ctx.mcp.call("recall_context", {"query": f"gate repo note {word}x"})).get("data") or {})
    mine = [x for x in facts.get("snippets") or [] if word in str(x.get("content")) and "x about" not in str(x.get("content"))]
    scopes = sorted({str(x.get("scope")) for x in mine})
    note_hits = [x for x in notes.get("snippets") or [] if f"{word}x" in str(x.get("content"))]
    ok = scopes == ["task"] and bool(note_hits) and not failed(note)
    return Outcome(ok, "the fact keeps scope task; the saved note is found by its own words",
                   f"fact scopes={scopes}; note saved={'no' if failed(note) else 'yes'}; note found={len(note_hits)} "
                   f"(sources: {sorted({str(x.get('source_type')) for x in notes.get('snippets') or []})})")


_IR = {"app": {"id": "gate-app", "name": "Gate"}, "functions": [
    {"name": "read_x", "action_type": "read", "steps": []},
    {"name": "make_note", "action_type": "write", "steps": [{"id": "s1", "op": "store.create",
                                                              "args": {"kind": "note", "data": {"t": "{{args.t}}"}}}]}]}


@scenario("ir-validators-agree", ("R1-10", "R1-13"), shells=("mcp",), account=True)
async def ir_validators_agree(ctx: Ctx) -> Outcome:
    v = await ctx.mcp.call("validate_ir", {"app_ir": _IR})
    s = await ctx.mcp.call("smoke_ir", {"app_ir": _IR, "function": "make_note", "args": {"t": "x"}})
    empty = await ctx.mcp.call("smoke_ir", {"app_ir": _IR, "function": "read_x", "args": {}})
    agree = failed(v) == failed(s)
    no_fake_write = data_of(empty).get("verified_write") is not True and "KeyError" not in text_of(s)
    return Outcome(agree and no_fake_write, "validate and smoke agree; no verified_write without a write",
                   f"validate={text_of(v)[:120]}; smoke={text_of(s)[:120]}; empty={text_of(empty)[:120]}")


@scenario("apps-unknown-tool-and-upload", ("R1-11", "R1-12"), shells=("mcp",), account=True)
async def apps_unknown_tool_and_upload(ctx: Ctx) -> Outcome:
    apps = data_of(await ctx.mcp.call("list_apps", {}))
    # the user's own app, else a system extension -- its tools come from the catalog (2026-11-15: a 404 for a system
    # extension's own record stopped run_*_tool before the catalog was asked)
    app_id = next((a.get("app_id") or a.get("id") for a in (apps.get("apps") or apps.get("items") or [])), "") or "notes"
    unk = await ctx.mcp.call("run_read_tool", {"app_id": app_id, "tool": "no_such_tool_gate", "args": {}})
    up = await ctx.mcp.call("upload_file", {"path": str(ctx.fixture.root / "README.txt")})
    said_none = re.search(r"no (such )?tool|not found|unknown tool|does not exist", text_of(unk), re.I) is not None
    doc = data_of(up).get("document_id")
    return Outcome(said_none and bool(doc), "unknown tool named as such; upload returns document_id",
                   f"unknown -> {text_of(unk)[:120]}; upload -> {text_of(up)[:120]}")


@scenario("savings-and-sizes", ("R1-16", "R1-18"), shells=("mcp",), account=True)
async def savings_and_sizes(ctx: Ctx) -> Outcome:
    sav = await ctx.mcp.call("token_savings", {})
    live = await ctx.mcp.call("live_sessions", {})
    frame = _frame(ctx, _session(), "Status?")
    ok = "saved" in text_of(sav).lower() and len(text_of(live)) <= LIVE_SESSIONS_CHARS_MAX \
        and len(frame) <= FRAME_CHARS_MAX
    return Outcome(ok, f"a saving shown; live_sessions <= {LIVE_SESSIONS_CHARS_MAX}; frame <= {FRAME_CHARS_MAX}",
                   f"savings={text_of(sav)[:100]}; live={len(text_of(live))}; frame={len(frame)}")


@scenario("webbee-reports-what-ran", ("R1-19",), shells=("webbee",), account=True)
async def webbee_reports_what_ran(ctx: Ctx) -> Outcome:
    """One real Webbee turn: its answer about a failing command must name the failure (the session log is the judge)."""
    text = await asyncio.to_thread(_webbee_once, ctx, "Run `git -C core/app show no-such-ref` and tell me its exit code.")
    return Outcome("128" in text, "the answer names exit 128", text[-300:])
