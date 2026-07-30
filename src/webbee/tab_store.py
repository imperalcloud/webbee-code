"""Tab persistence per-repo (0.3.37): remembers WHICH tabs were open so
closing the terminal is no longer destructive -- reopen it and your 3 tabs
come back, each one re-attaching to its own still-Running Temporal workflow
when there is one, or restoring just its label/mode/draft when there is not.

Deliberately modelled on `mode_store` (same house pattern, same failure
posture) with one difference: a tab is a RECORD, not a single string, so the
file is JSONL -- one JSON object per line, newest write replaces the file.
JSONL (not one big JSON doc) so a single corrupt line costs ONE tab instead
of the whole layout.

Fail-soft in BOTH directions, by design:
  * `load_tabs` -- missing file, unreadable dir, corrupt/garbage lines all
    degrade to [] (or skip just the bad line). A bad cache is exactly as safe
    as no cache: you get today's behaviour, a single fresh tab.
  * `save_tabs` -- a write failure (read-only home, disk full) is silently
    dropped. Losing the memory is far smaller than crashing the terminal over
    a nice-to-have.

WHAT IS PERSISTED, and what deliberately is NOT:
  * persisted: `session_id` (the re-attach handle -- the whole point),
    `label`, `mode`, `workspace`, `draft`.
  * NEVER persisted: the transcript (it lives server-side and replays on
    re-attach -- duplicating it here would risk showing a stale copy), and
    `autopilot` as a mode. Autopilot auto-approves every tool call, so it is
    downgraded to 'default' on write for EXACTLY the reason mode_store
    downgrades it: resuming it silently from a stale file is unsafe.
"""
from __future__ import annotations

import json
import os

from webbee.repo import compute_repo_key, find_repo_root

_CACHE_DIR = os.path.expanduser("~/.cache/webbee")   # test seam: monkeypatch this name

MAX_TABS = 12   # a sane ceiling: a corrupt/huge file can never spawn tabs forever

_KEY_CACHE: dict[str, str] = {}


def _repo_key_for(workspace: str) -> str:
    """Memoised exactly like mode_store's twin: `compute_repo_key` shells out
    to git, and this is reached from UI paths where a stall would be felt."""
    key = _KEY_CACHE.get(workspace)
    if key is None:
        key = compute_repo_key(find_repo_root(workspace))
        _KEY_CACHE[workspace] = key
    return key


def _path_for(workspace: str) -> str:
    return os.path.join(_CACHE_DIR, f"tabs-{_repo_key_for(workspace)}.jsonl")


def tab_record(session_id: str = "", label: str = "", mode: str = "default",
               workspace: str = "", draft: str = "", created_at: float = 0.0) -> dict:
    """PURE. Build ONE normalised tab record. Central so the writer and the
    tests agree on the shape, and so the autopilot downgrade can never be
    forgotten at a call site.

    `created_at` (home-tab-durations-v1): WALL-CLOCK epoch seconds the tab was
    first opened -- persisted (unlike SessionSlot.started_at, which is
    monotonic and meaningless across a process restart) so a restored tab
    keeps its TRUE original age instead of resetting to "just now". 0.0 (the
    default) means unknown -- an old record from before this field existed
    degrades to "no duration shown", never a fake age."""
    return {
        "session_id": str(session_id or ""),
        "label": str(label or ""),
        "mode": "default" if str(mode or "") == "autopilot" else (str(mode or "") or "default"),
        "workspace": str(workspace or ""),
        "draft": str(draft or ""),
        "created_at": float(created_at or 0.0),
    }


def load_tabs(workspace: str) -> list:
    """The remembered tabs for `workspace`'s repo, oldest-first, or [] on no
    file / ANY error. Individual corrupt lines are SKIPPED, not fatal: one bad
    line must never cost you the other two tabs. Capped at MAX_TABS."""
    out: list = []
    try:
        with open(_path_for(workspace), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue          # one unreadable tab, not a broken layout
                if isinstance(rec, dict):
                    out.append(tab_record(**{
                        **{k: rec.get(k, "") for k in
                           ("session_id", "label", "mode", "workspace", "draft")},
                        "created_at": rec.get("created_at", 0.0),
                    }))
                if len(out) >= MAX_TABS:
                    break
    except Exception:
        return []
    return out


def save_tabs(workspace: str, tabs: list) -> None:
    """Remember `tabs` (a list of dicts/records, oldest-first) for this repo.
    Written whole (replace, not append) so the file always mirrors the CURRENT
    layout -- a closed tab genuinely disappears. Never raises."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        lines = []
        for t in list(tabs)[:MAX_TABS]:
            if not isinstance(t, dict):
                continue
            lines.append(json.dumps(tab_record(**{k: t.get(k, "") for k in
                                                  ("session_id", "label", "mode",
                                                   "workspace", "draft")}),
                                    ensure_ascii=False))
        with open(_path_for(workspace), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
    except Exception:
        pass


def clear_tabs(workspace: str) -> None:
    """Forget this repo's remembered layout (a clean `/exit` of the LAST tab,
    or a user who wants a fresh dock). Never raises."""
    try:
        os.remove(_path_for(workspace))
    except Exception:
        pass
