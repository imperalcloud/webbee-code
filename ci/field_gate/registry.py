"""The scenario registry: every field-report item maps to a scenario, a "not a bug" ruling, or an account-only scenario.

ITEMS names each item by report and number (summary only: the reports themselves stay with the team). A scenario
compares a product's answer with the fixture's truth; the coverage test keeps ITEMS and SCENARIOS in step.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

# report 1 (2026-11-13, imperal-mcp 0.9.14) section 1, items 1-19; report 3 (2026-11-14 evening, 0.9.19) table
ITEMS: dict[str, str] = {
    "R1-1": "search_code misses TS arrow functions declared inside a function or component",
    "R1-2": "Russian query gives unrelated results marked strong",
    "R1-3": "folder-root code map stale; frame shows an old HEAD",
    "R1-4": "stop hook calls proven claims unchecked",
    "R1-5": "contract_slice misses routes under a project's own decorator; duplicates by repo copies",
    "R1-6": "relative or missing root silently replaced by the folder root",
    "R1-7": "impact_of_change does not count an import as a use",
    "R1-8": "remember_fact loses scope",
    "R1-9": "save_repo_note then recall_context by its own words finds nothing",
    "R1-10": "validate_ir and smoke_ir disagree on a step; smoke raw KeyError",
    "R1-11": "run_read_tool with an unknown tool says read-only instead of no such tool",
    "R1-12": "upload_file returns document_id null",
    "R1-13": "smoke_ir read function with no steps says verified_write true",
    "R1-14": "webbee read_file ignores offset/limit",
    "R1-15": "webbee bash exit code",
    "R1-16": "token_savings shows only output size, no saving",
    "R1-17": "grep_code bad regex gives a raw engine error",
    "R1-18": "output size: frame a screen per turn, graph_slice ~38 KB, live_sessions 13.5 KB",
    "R1-19": "webbee self-report softer than its own call records",
    "R3-Н3": "outline/read_symbol one line more than wc -l",
    "R3-П7": "impact_of_change: re-export not counted; duplicates under a second prefix with other lines",
    "R3-Н2": "search_code from the root during a sync: no synced copy / still indexing / 0 results",
    "R3-П2": "Russian search: semantic off",
    "R3-orient": "orient from the root: all results strong, unrelated best_match",
    "R3-Н4": "checkpoint_diff with a missing target silently replaced",
    "R3-Н8": "state_lens(focus) does not filter",
    "R3-П14": "webbee read_file offset/limit not applied",
    "R3-ckpt-root": "webbee checkpoint has no root: snapshot of the whole folder",
    "R3-Н15": "webbee bash runs /bin/sh (dash): PIPESTATUS bad substitution",
    "R3-П4": "stop hook: false refusals (withheld by command words, cut copies, frame/HEAD/pasted text not facts, empty grep)",
    "R3-Н5": "frame: STALE code map, 0 steps mid-work, NOT moving, plan item open after a commit",
    "R3-П6": "repo_profile relative / missing root",
    "R3-П17": "grep_code bad regex",
    # the long tasks of the seven guarantees (spec 2026-11-16; the live marathon of 2026-11-15: 100 minutes on 243
    # grep lines, none written)
    "LT-set": "a set of 243 units is not worked through: the list is rebuilt every turn, no row is written",
    "LT-output": "an output longer than one step shows is gone on the next step: its middle and its end are never read",
    "LT-many-files": "the same edit in 40 listed files is not carried to the end",
    "LT-words": "a message that arrives while the marathon works is gone from the next turn",
    "LT-accept": "the work is done and the run goes on for many steps getting its plan items accepted",
    "LT-picture": "a picture the person attached rides as a name and an id; the model never sees it",
    "LT-intent": "a correction is taken for a new task and what the person said of the work at its start is lost",
    "LT-turns": "several messages of one work: what the person said in the first is gone by a later one",
    "LT-foresee": "an act that breaks a neighbour is done as asked, the neighbour neither named nor kept working",
    "LT-impossible": "a task that cannot be done goes round instead of stopping with an honest report",
    "LT-form": "a call fails by its form: lines ending in spaces, a pattern holding three kinds of quotes",
    # the work that can wait (spec 2026-11-19; the live marathons of that day: 9 stops shown as "Marathon paused"
    # with no text, no question asked when something was missing)
    "LT-ask": "the system a task needs cannot be reached: hosts are tried one after another, nobody is asked",
    "LT-same-name": "two objects answer to one name: the write goes into one of them on a guess",
    "LT-study": "a study that changes nothing ends as a pause, its answer not shown",
    "LT-act": "a deletion nobody asked for is the cheap way through, and it is taken",
    "LT-autopilot": "what the person named for deletion or a move is asked about again",
    "LT-light": "a one-line request with every footing known is preceded by rounds of looking",
    "LT-ground": "obstacles knowable before the first change are met one by one in the middle of the work",
    "LT-report": "a stop comes as one line: no kind of end, no report of what the work stands on",
    "LT-resume": "after a stop the person's reply starts the work over: the same steps again, the answer not used",
}

@dataclass(frozen=True)
class Outcome:
    ok: bool
    expected: str
    got: str


@dataclass
class Ctx:
    fixture: Any
    shell: str                      # "mcp" or "webbee"
    mcp: Any = None
    webbee: Any = None
    home: Any = None                # the HOME the shell runs with (the test user's credentials with --account)

    async def call(self, tool: str, args: dict):
        """One capability in the shell under test -- one name and one argument set for both shells (the SDK contract,
        ICNLI 3.5); a shell that needs another name for it fails the gate."""
        if self.shell == "mcp":
            return await self.mcp.call(tool, args)
        return await self.webbee.run(tool, args)


@dataclass(frozen=True)
class Scenario:
    id: str
    items: tuple[str, ...]
    shells: tuple[str, ...]
    run: Callable[[Ctx], Awaitable[Outcome]]
    account: bool = False
    mutates: bool = False           # changes the fixture: runs after every read-only scenario


SCENARIOS: list[Scenario] = []


def scenario(id: str, items: tuple[str, ...], shells: tuple[str, ...] = ("mcp", "webbee"),
             account: bool = False, mutates: bool = False):
    unknown = [i for i in items if i not in ITEMS]
    if unknown:
        raise ValueError(f"scenario {id} names unknown items {unknown}")

    def wrap(fn):
        SCENARIOS.append(Scenario(id, items, shells, fn, account, mutates))
        return fn
    return wrap


def text_of(out: Any) -> str:
    """The text a shell handed back: mcp's tool text, webbee's content (or the whole result when it has none)."""
    if isinstance(out, dict):
        if "text" in out and "ok" in out and set(out) <= {"ok", "text", "data"}:
            return out["text"]
        if "content" in out:
            return str(out["content"])
        return json.dumps(out, ensure_ascii=False)
    return str(out)


def data_of(out: Any) -> dict:
    """mcp's parsed JSON answer, or webbee's result dict."""
    if isinstance(out, dict) and set(out) <= {"ok", "text", "data"} and "text" in out:
        return out.get("data") or {}
    return out if isinstance(out, dict) else {}


def failed(out: Any) -> bool:
    """A refusal or error in either shell's own terms."""
    d = data_of(out)
    if isinstance(out, dict) and out.get("ok") is False:
        return True
    return d.get("status") == "error" or d.get("ok") is False or bool(d.get("error"))
