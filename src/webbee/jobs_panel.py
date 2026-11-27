"""The background strip: a narrow panel pinned above the input that shows the session's jobs on this machine and
lets the person act on one (the owner, 2026-11-27: "a narrow, handy field above the prompt where you see that
something is in the background and can interact with it -- neat, useful, without breaking the look").

It follows the queue strip (``queue_panel``) in everything that makes that one safe: the builders are PURE, the
dock mounts them in a ConditionalContainer that takes ZERO rows while there is nothing to show, each row is one
line, a row's mouse handler fires on MOUSE_UP only and gives the output pane first refusal (a drag released here is
not a click). So a session with no job looks exactly as before.

    ⚙ background (2) ▾ · click a job for its output · ✕ ends it
       j2 · done · npm run build
       j3 · running 2:14 · make test                             ✕

A row is a job that runs, or one that ended a short while ago (so its end is seen without scrolling the output);
the newest stands nearest the input. A click on a row prints that job's output so far; ✕ on a running one ends it."""
import time

from webbee.queue_panel import _item_handler, _toggle_handler, one_line

JP_MAX_ITEMS = 3          # rows shown; more add one "… +K more" row -- the strip stays narrow
RECENT_S = 600.0          # an ended job stays in the strip this long
_STATE = {"running": "running", "done": "done", "failed": "failed", "cancelled": "cancelled", "lost": "lost",
          "unknown": "ended"}


def shown(rows, now: float = 0.0, recent_s: float = RECENT_S) -> list:
    """PURE. The jobs the strip shows, oldest first: every running one, and one that ended within ``recent_s``."""
    now = now or time.time()
    out = []
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        if r.get("state") == "running" or now - float(r.get("ended_at") or 0.0) <= recent_s:
            out.append(r)
    return sorted(out, key=lambda r: int(r.get("n") or 0))


def jobs_height(rows, collapsed: bool = False, max_items: int = JP_MAX_ITEMS, now: float = 0.0) -> int:
    """PURE. Rows the strip takes: 0 with nothing to show, 1 collapsed, else the header and one per shown job (and
    one "+K more" row for the rest)."""
    n = len(shown(rows, now))
    if not n:
        return 0
    if collapsed:
        return 1
    return 1 + min(n, max_items) + (1 if n > max_items else 0)


def _elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def row_text(job: dict, now: float) -> str:
    """PURE. One job as its row says it: id, state (a running one with how long), what it is."""
    state = str(job.get("state") or "")
    word = _STATE.get(state, state or "?")
    if state == "running" and job.get("started"):
        word += " " + _elapsed(now - float(job["started"]))
    elif state == "failed" and job.get("exit_code") is not None:
        word += f" (exit {job['exit_code']})"
    what = str(job.get("title") or job.get("command") or "").strip()
    return f"{job.get('id') or 'j?'} · {word}" + (f" · {what}" if what else "")


def jobs_fragments(rows, width: int = 0, collapsed: bool = False, toggle=None, open_job=None, end_job=None,
                   max_items: int = JP_MAX_ITEMS, forward=None, now: float = 0.0) -> list:
    """PURE builder: the strip as prompt_toolkit formatted text, re-made at every redraw. ``open_job(n)`` /
    ``end_job(n)`` are the dock's actions for a click on a row and on its ✕; without them the rows are plain."""
    now = now or time.time()
    jobs = shown(rows, now)
    if not jobs:
        return []
    running = sum(1 for j in jobs if j.get("state") == "running")
    marker = "" if toggle is None else (" ▸" if collapsed else " ▾")
    head = f" ⚙ background ({running} running)" if running else f" ⚙ background ({len(jobs)} ended)"
    header = ("class:jp.header", head + marker)
    if toggle is not None:
        header = header + (_toggle_handler(toggle, forward),)
    frags = [header]
    if collapsed:
        return frags
    if open_job is not None:
        frags.append(("class:jp.item", " · click a job for its output" + (" · ✕ ends it" if end_job and running else "")))
    start = max(0, len(jobs) - max_items)
    if start:
        frags.append(("class:jp.item", f"\n   … +{start} more · /jobs lists them all"))
    for job in jobs[start:]:
        n = int(job.get("n") or 0)
        live = job.get("state") == "running"
        can_end = live and end_job is not None
        avail = (width - 4 - (2 if can_end else 0)) if width > 0 else 0
        text = "\n   " + one_line(row_text(job, now), avail)
        style = "class:jp.live" if live else ("class:jp.bad" if job.get("state") in ("failed", "lost") else "class:jp.item")
        if open_job is None:
            frags.append((style, text))
        else:
            frags.append((style, text, _item_handler(open_job, n, forward)))
        if can_end:
            frags.append(("class:jp.end", " ✕", _item_handler(end_job, n, forward)))
    return frags
