"""Scenarios that need no account: code tools, checkpoints, the shell -- each answer against the fixture's truth."""
from __future__ import annotations

import asyncio
import re

from ci.field_gate.registry import Ctx, Outcome, data_of, failed, scenario, text_of

INDEX_WAIT_S = 120      # an index that is still building may say so; the code scenarios judge the answer once it is built


async def _settled(ctx: Ctx, tool: str, args: dict):
    """Ask until the answer no longer says it is still indexing (that honesty is R3-Н2's own scenario)."""
    deadline = asyncio.get_running_loop().time() + INDEX_WAIT_S
    while True:
        out = await ctx.call(tool, args)
        if data_of(out).get("status") != "partial" or asyncio.get_running_loop().time() > deadline:
            return out
        await asyncio.sleep(3)


def _items(out) -> list[dict]:
    """The listed results in either shell's shape: mcp's named list, webbee's data.items."""
    d = data_of(out)
    for key in ("contracts", "matches", "items"):
        if isinstance(d.get(key), list):
            return d[key]
    inner = d.get("data") if isinstance(d.get("data"), dict) else {}
    return inner.get("items") or []


_LINES_SAID = re.compile(r"\((\d+) lines;|· (\d+) lines|total_lines\D+(\d+)|of (\d+)\]")


def _lines_said(out) -> int | None:
    d = data_of(out)
    if isinstance(d.get("total_lines"), int):
        return d["total_lines"]
    m = _LINES_SAID.search(text_of(out))
    return int(next(g for g in m.groups() if g)) if m else None


@scenario("lines-outline", ("R3-Н3",))
async def lines_outline(ctx: Ctx) -> Outcome:
    t = ctx.fixture.truth
    wrong = []
    for rel in ("core/app/mid.py", "core/app/no_nl.py", "core/app/crlf.py", "core/app-gateway/big.py"):
        said = _lines_said(await ctx.call("outline", {"path": rel}))
        if said != t.lines[rel]:
            wrong.append(f"{rel}: {said} (wc -l {t.lines[rel]})")
    return Outcome(not wrong, "outline total lines == the file's lines", "; ".join(wrong) or "all equal")


@scenario("lines-read", ("R3-Н3",))
async def lines_read(ctx: Ctx) -> Outcome:
    t = ctx.fixture.truth
    wrong = []
    for rel in ("core/app/mid.py", "core/app/no_nl.py"):
        out = await ctx.call("read_symbol", {"path": rel, "name": "L1-2"})
        m = re.search(r"of (\d+)\]|· (\d+) lines", text_of(out))
        said = int(next(g for g in m.groups() if g)) if m else None
        if said != t.lines[rel]:
            wrong.append(f"{rel}: {said} (wc -l {t.lines[rel]})")
    return Outcome(not wrong, "read_symbol names the file's line count", "; ".join(wrong) or "all equal")


@scenario("search-nested-arrow", ("R1-1",))
async def search_nested_arrow(ctx: Ctx) -> Outcome:
    p = ctx.fixture.truth.nested_ts
    out = await _settled(ctx, "search_code", {"query": p.symbol})
    hit = p.path in text_of(out)
    return Outcome(hit, f"{p.symbol} found in {p.path}", text_of(out)[:300])


@scenario("contract-own-decorator", ("R1-5",))
async def contract_own_decorator(ctx: Ctx) -> Outcome:
    p = ctx.fixture.truth.door_route
    out = await _settled(ctx, "contract_slice", {"symbols": [p.symbol]})
    found = [(c.get("method"), c.get("route"), c.get("handler")) for c in _items(out) if c.get("route") == p.text]
    return Outcome(len(found) == 1, f"route {p.text} of {p.symbol} exactly once", f"{len(found)}: {found}")


@scenario("root-relative-and-missing", ("R1-6", "R3-П6"))
async def root_relative_and_missing(ctx: Ctx) -> Outcome:
    files = ctx.fixture.truth.git_files["app"]
    out = await ctx.call("repo_profile", {"root": "core/app"})
    profile = data_of(out).get("profile") or (_items(out) or [{}])[0]
    count = profile.get("file_count")
    missing = await ctx.call("repo_profile", {"root": "core/no-such-repo"})
    ok = count == len(files) and failed(missing)
    return Outcome(ok, f"core/app: {len(files)} files; missing root refused",
                   f"file_count={count}; missing root -> {text_of(missing)[:160]}")


@scenario("impact-reexport", ("R1-7", "R3-П7"))
async def impact_reexport(ctx: Ctx) -> Outcome:
    r = ctx.fixture.truth.reexport
    out = await _settled(ctx, "impact_of_change", {"symbols": [r.symbol]})
    text = text_of(out)
    want = [r.caller_path, r.reexport_path]
    absent = [w for w in want if w not in text and w.split("/", 1)[-1] not in text]
    paths = set(re.findall(r"[\w./-]+\.py", text))
    doubled = sorted(p for p in paths for q in paths if p != q and q.endswith("/" + p))
    ok = not absent and not doubled
    return Outcome(ok, f"{r.symbol}: users {want}, each path once",
                   f"missing {absent}; listed under two prefixes {doubled}")


@scenario("grep-bad-regex", ("R1-17", "R3-П17"))
async def grep_bad_regex(ctx: Ctx) -> Outcome:
    out = await ctx.call("grep_code", {"pattern": "def (", "regex": True})
    text = text_of(out)
    ok = failed(out) and "regex=false" in text
    return Outcome(ok, "a clear error naming regex=false", text[:200])


@scenario("diff-missing-target", ("R3-Н4",))
async def diff_missing_target(ctx: Ctx) -> Outcome:
    out = await ctx.call("checkpoint_diff", {"target": 99})
    return Outcome(failed(out), "an error naming the checkpoints that exist", text_of(out)[:200])


@scenario("read-window", ("R1-14", "R3-П14"), shells=("webbee",))
async def read_window(ctx: Ctx) -> Outcome:
    out = await ctx.call("read_file", {"path": "core/app/mid.py", "offset": 150, "limit": 40})
    text = text_of(out)
    ok = "value_150 = 150" in text and "value_189 = 189" in text and "value_149 =" not in text \
        and "value_190 =" not in text
    return Outcome(ok, "exactly lines 150-189", text[:200])


@scenario("bash-is-bash", ("R1-15", "R3-Н15"), shells=("webbee",))
async def bash_is_bash(ctx: Ctx) -> Outcome:
    pipe = await ctx.call("bash", {"command": "false | true; echo ${PIPESTATUS[0]}"})
    code = await ctx.call("bash", {"command": "exit 3"})
    ok = text_of(pipe).strip().endswith("1") and data_of(code).get("exit_code") == 3
    return Outcome(ok, "PIPESTATUS works; exit 3 reported as 3",
                   f"pipe -> {text_of(pipe).strip()[:80]}; exit 3 -> {data_of(code).get('exit_code')}")


@scenario("checkpoint-root", ("R3-ckpt-root",), mutates=True)
async def checkpoint_root(ctx: Ctx) -> Outcome:
    root = ctx.fixture.root
    await ctx.call("checkpoint", {"label": "gate-root", "root": "core/app"})
    inside, outside = root / "core/app/mid.py", root / "lib-sdk/lib_sdk/emit.py"
    before = inside.read_bytes(), outside.read_bytes()
    inside.write_bytes(before[0] + b"gate_inside = 1\n")
    outside.write_bytes(before[1] + b"gate_outside = 1\n")
    try:
        out = await ctx.call("checkpoint_diff", {"root": "core/app"})
    finally:
        inside.write_bytes(before[0])
        outside.write_bytes(before[1])
    text = text_of(out)
    ok = "gate_inside" in text and "gate_outside" not in text
    return Outcome(ok, "a checkpoint of core/app sees core/app only", text[:200])


@scenario("rollback-nested", ("R3-ckpt-root",), mutates=True)
async def rollback_nested(ctx: Ctx) -> Outcome:
    f = ctx.fixture.root / "core/app/no_nl.py"
    before = f.read_bytes()
    cp = await ctx.call("checkpoint", {"label": "gate-rb"})
    ref = data_of(cp).get("id") or re.search(r"cp-\d+", text_of(cp)).group(0)
    f.write_bytes(before + b"\ngate = 1\n")
    out = await ctx.call("checkpoint_rollback", {"target": ref, "paths": ["core/app/no_nl.py"]})
    restored = f.read_bytes() == before
    f.write_bytes(before)
    return Outcome(restored, "a file in a nested repo rolled back by paths", text_of(out)[:200])
