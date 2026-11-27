# Frame v2 (Slice-5 T8/T9, dual-emit compat window) -------------------------
# step_started/step_finished are the FACTS-ONLY twin of the legacy 'action'
# start/done frames. For EXT tools the kernel reuses the SAME id (tc["id"])
# as both the old vocabulary's step_id and the new vocabulary's step_id, so
# a single `started`/`finished` id-set pair dedups correctly across BOTH
# vocabularies regardless of arrival order -- required because the kernel
# dual-emits both during the compat window and a naive client would double
# sink.tool_start/tool_result (inflating the toolbar's "N actions" count and
# double-printing the result line).
#
# LOCAL tools are the one case where id-dedup does NOT apply: the reverse
# channel's tool_request/result round trip is keyed by a SEPARATE,
# server-generated req_id ("req-{session_id}-{n}",
# coding_agent_workflow._dispatch_local_raw) that never equals step_id
# (tc["id"]). handle_step_started/handle_step_finished treat kind ==
# "local_tool" as a pure no-op for that reason -- see their docstrings.


def _v2_step_label(frame: dict) -> str:
    """Renderer-composed label for a v2 step -- the SAME app_id·tool ladder
    the old 'action' frame already uses (I-STREAM-STEP-LABEL-USER-LANG).
    step_started carries the same app_id/tool fields, just under the
    facts-only vocabulary; local tools have no app_id and degrade to the
    bare tool name."""
    return "·".join(x for x in (frame.get("app_id", ""), frame.get("tool", "")) if x)


def _summary(result: dict) -> str:
    """One-line summary of a tool result for the UI."""
    content = str(result.get("content", ""))
    first = content.strip().splitlines()[0] if content.strip() else ""
    return first[:120]


def _summary_from_facts(facts: dict) -> str:
    """Renderer-composed one-line summary from v2 structured summary_facts
    (I-FRAMES-FACTS-ONLY: the kernel emits facts, never prose here). Degrades
    gracefully to a bare count when entity_kind is empty -- today's kernel
    call sites don't set it yet (Slice-5 T8)."""
    facts = facts or {}
    count = facts.get("count")
    if count is None:
        return ""
    if facts.get("offset") is not None and str(facts.get("entity_kind") or "") == "char":
        # a page of a stored output: WHICH part of it -- the same page read twice shows the same numbers twice
        try:
            start, got = int(facts.get("offset") or 0), int(count)
            whole = facts.get("total")
            return f"chars {start}-{start + got}" + (f" of {int(whole)}" if whole is not None else "")
        except (TypeError, ValueError):
            pass
    kind = str(facts.get("entity_kind") or "").strip()
    if not kind:
        return str(count)
    return f"{count} {kind}" if count == 1 else f"{count} {kind}s"


def _progress_text(frame: dict) -> str:
    """Dual-read: v2 progress frames carry BOTH 'llm_text' (canonical) and
    'text' (legacy) during the compat window; a v1-only frame carries only
    'text'. Prefer llm_text when present."""
    return str(frame.get("llm_text") or frame.get("text") or "")


def thinking_parts(frame: dict, tag: str = "") -> dict:
    """A thinking frame's named parts (the kernel's ``thought_frame``: ``kind``, ``say``, ``lines``) as a sink
    takes them; ``{}`` for a frame that names none (an older kernel) -- that one is drawn whole, as before."""
    kind = frame.get("kind") if isinstance(frame, dict) else None
    if not (isinstance(kind, str) and kind):
        return {}
    say = str(frame.get("say") or "")
    lines = frame.get("lines")
    return {"kind": kind, "say": tag + say if say else "", "lines": lines if isinstance(lines, dict) else {}}


def show_tool_result(sink, tool: str, ok: bool, summary: str, args, result) -> None:
    """The result line of an action the client ran itself, with what a click on it opens: the whole arguments and
    the whole result. A sink that takes no detail (a plain one, an older one) gets the line as before."""
    try:
        sink.tool_result(tool, ok, summary, detail={"args": args, "result": result})
    except TypeError:
        sink.tool_result(tool, ok, summary)


def show_thinking_live(sink, tag: str, frame: dict) -> None:
    """A thought while it is still being written (frame ``thinking_delta``): ``say`` holds the words for the person
    written SO FAR, whole each time -- a frame lost or repeated on a reconnect changes nothing. It is shown on the
    live line only; the finished thought comes as the ``thinking`` frame and is the one kept in the transcript. A
    provider's own reasoning is not shown, as it is not when finished; a sink with no live line shows nothing."""
    live = getattr(sink, "thinking_live", None)
    if live is None or not isinstance(frame, dict) or frame.get("kind") == "native":
        return
    say = str(frame.get("say") or "")
    if say:
        try:
            live(tag + say)
        except Exception:
            pass


def show_thinking(sink, tag: str, text: str, tier: str, frame: dict) -> None:
    """Hand a thinking frame to the sink: by its parts when the frame names them and the sink can take them -- the
    words for the person large, the step's protocol small, a provider's own reasoning not at all; whole, as
    before, for an older frame or a sink that knows no parts; as plain progress for a sink with no thinking."""
    shown = tag + text if text else ""
    thinking = getattr(sink, "thinking", None)
    if thinking is None:
        if isinstance(frame, dict) and frame.get("kind") == "native":
            return                       # a provider's own reasoning is drawn nowhere, a plain sink included
        sink.progress(shown)
        return
    try:
        thinking(shown, tier=tier, **thinking_parts(frame, tag))
    except TypeError:
        try:
            thinking(shown, tier=tier)
        except TypeError:
            thinking(shown)


def _first_time(step_id: str, seen_ids: set) -> bool:
    """True the first time step_id is seen across EITHER vocabulary, then
    remembers it. An empty step_id never dedups (some legacy frames omit
    it) -- treated as always-first."""
    if not step_id:
        return True
    if step_id in seen_ids:
        return False
    seen_ids.add(step_id)
    return True


def _server_step_result(sink, label: str, ok: bool, summary: str, sid: str, args, fetch) -> None:
    """The result line of an action the SERVER ran. With a way to ask for its record (``fetch(step_id)``) the line
    opens on a click (webbee.expand.RemoteStep); without one, or for a sink that takes no detail, as before."""
    if fetch is not None and sid:
        try:
            sink.tool_result(label, ok, summary, detail={"args": args or {}, "remote": lambda: fetch(sid)})
            return
        except TypeError:
            pass
    sink.tool_result(label, ok, summary)


def handle_step_started(frame: dict, sink, started: set, step_labels: dict, local_ids: set,
                        step_args=None) -> None:
    """v2 step_started -> sink.tool_start, deduped by step_id against the
    OLD vocabulary's start (action-start).

    LOCAL tools (kind == "local_tool") are a NO-OP here: the kernel's local
    reverse channel issues its OWN sequential req_id
    (``req-{session_id}-{n}``, coding_agent_workflow._dispatch_local_raw) --
    a SEPARATE id space from step_id (tc["id"], the LLM's tool_call id), so
    step_id and the tool_request's req_id never match and can't be deduped
    by id. The paired tool_request/result frames (unchanged, real args)
    remain the SOLE renderer for local tools; rendering the v2 twin too
    would double the step."""
    sid = str(frame.get("step_id", "") or "")
    if frame.get("kind") == "local_tool":
        if sid:
            local_ids.add(sid)
        return
    label = _v2_step_label(frame)
    if sid:
        step_labels[sid] = label
        if step_args is not None and isinstance(frame.get("args"), dict):
            step_args[sid] = frame["args"]        # what was asked, kept for the opened line
    if _first_time(sid, started):
        sink.tool_start(label, frame.get("args") or {})


def handle_step_finished(frame: dict, sink, finished: set, step_labels: dict, steps: list,
                         local_ids: set, step_args=None, fetch=None) -> None:
    """v2 step_finished -> sink.tool_result + steps append, deduped by
    step_id against the OLD vocabulary's finish (action-done). Renders the
    summary from structured summary_facts -- never prose from the kernel.
    A step whose start was a local-tool no-op (see handle_step_started) is
    ALSO a no-op here -- the tool_request round trip already rendered it."""
    sid = str(frame.get("step_id", "") or "")
    if sid in local_ids:
        local_ids.discard(sid)
        return
    if not _first_time(sid, finished):
        return
    label = step_labels.pop(sid, sid)
    ok = bool(frame.get("ok"))
    summary = _summary_from_facts(frame.get("summary_facts") or {})
    _server_step_result(sink, label, ok, summary, sid, (step_args or {}).pop(sid, None), fetch)
    steps.append({"step_id": sid, "label": label, "ok": ok})


def handle_tool_request(frame: dict, executor) -> dict:
    """Run a (kernel-pre-approved) local tool. The kernel gates write/bash
    consent via confirm_request BEFORE dispatching, so a tool_request here is
    always cleared to run. NEVER raises: a tool that errored (or a bug in the
    executor) must STILL return a result so the caller posts it back and the
    kernel's dispatch unblocks — an unposted result hangs the turn and freezes
    the whole dock."""
    rid = frame.get("req_id")
    try:
        result = executor.run(frame.get("tool", ""), frame.get("args", {}))
    except Exception as e:
        result = {"ok": False, "content": f"local tool crashed: {type(e).__name__}: {e}"}
    return {"req_id": rid, "result": result}


def handle_action_frame(frame: dict, sink, started: set, finished: set, steps: list, fetch=None) -> None:
    """OLD R2 ext-tool 'action' start/done frame -> sink.tool_start/
    tool_result, deduped by step_id against the v2 twin (dual-emit compat
    window, Slice-5 T9)."""
    lbl = "·".join(x for x in (frame.get("app_id", ""), frame.get("tool", "")) if x)
    sid = str(frame.get("step_id", "") or "")
    if frame.get("phase") == "start":
        if _first_time(sid, started):
            sink.tool_start(lbl, {})
        return
    if not _first_time(sid, finished):
        return
    summ = str(frame.get("summary", "") or "")
    if summ in ("None", "none"):  # tool result had no content — clean ✓
        summ = ""
    ok = bool(frame.get("ok"))
    _server_step_result(sink, lbl, ok, summ, sid, None, fetch)
    steps.append({"step_id": sid, "label": lbl, "ok": ok})


# --- Cross-surface (foreign-turn) frames -------------------------------------
# Frames stamped with a DIFFERENT task_id belong to another turn on the shared
# persistent stream -- a turn steered from Telegram/the panel (kernel stamps
# `origin` with the source surface) or a stale prior turn (no `origin`). They
# are DISPLAY-ONLY for this client: one tagged line, NEVER executed, NEVER
# consented, NEVER terminal for the client's own turn (the C7 safety filter in
# session.run() owns that guarantee; these helpers only compose the line).

_FOREIGN_ACTIONABLE_TYPES = ("tool_request", "confirm_request", "final",
                             "marathon_complete", "panel_release_required")


def _is_foreign_frame(frame: dict, own_task_id: str) -> bool:
    """C7 filter predicate (extracted verbatim from run()'s inline check): a
    frame stamped with a DIFFERENT task_id on the shared persistent stream,
    when it is origin-stamped or actionable, belongs to another turn and is
    DISPLAY-ONLY for this client. task_id absent (legacy kernels) -> own."""
    ftid = frame.get("task_id", "")
    return bool(ftid and own_task_id and ftid != own_task_id and (
        frame.get("origin") or frame.get("type") in _FOREIGN_ACTIONABLE_TYPES))


def _origin_tag(frame: dict) -> str:
    """Display prefix for an OWN-turn frame steered from another surface.
    Live steer topology: a Telegram/panel-steered turn keeps the terminal's
    OWN task_id (the terminal stays the sole executor) -- only the kernel-
    stamped `origin` says where it came from. Empty for terminal/own frames,
    so untagged rendering stays byte-identical to today."""
    origin = str(frame.get("origin", "") or "")
    return f"[{origin}] " if origin and origin != "terminal" else ""


def _foreign_note(frame: dict) -> str:
    """One-line display text for a cross-surface frame, or "" when there is
    nothing meaningful to show (usage/step bookkeeping/unknown types) -- the
    caller then skips it silently."""
    ftype = frame.get("type", "")
    tool = str(frame.get("tool", "") or "")
    if ftype == "tool_request":
        return f"running {tool}" if tool else "running a tool"
    if ftype == "confirm_request":
        return f"approval requested: {tool}" if tool else "approval requested"
    if ftype == "thinking":
        # a frame that names its parts: the words for the person; the whole text for a step that wrote none
        # (so the step does not vanish from the note); nothing for a provider's own reasoning. A frame that names
        # no parts (an older kernel) is shown whole, as before
        parts = thinking_parts(frame)
        if parts:
            return "" if parts["kind"] == "native" else (parts["say"].strip() or _progress_text(frame).strip())
        return _progress_text(frame).strip()
    if ftype in ("final", "marathon_complete", "progress"):
        return _progress_text(frame).strip()
    if ftype == "panel_release_required":
        summary = str(frame.get("summary", "") or "").strip()
        return summary or "payment approval required in the panel"
    return ""


def render_foreign_frame(frame: dict, sink) -> None:
    """Render ONE tagged, display-only line for a foreign-turn frame. Guarded
    like the marathon notes: a minimal sink without `foreign_turn` drops the
    line, and a render error must never break the safety `continue` in
    session.run() (rendering is the ONLY thing foreign frames ever get)."""
    text = _foreign_note(frame)
    render = getattr(sink, "foreign_turn", None)
    if not text or render is None:
        return
    try:
        render(str(frame.get("origin", "") or ""), "assistant", text)
    except Exception:
        pass


# --- U4 marathon FACT frames -------------------------------------------------
# A marathon (long-horizon autonomous run) streams the SAME frame vocabulary as
# a coding turn PLUS three progress FACTS. The kernel emits facts; this renderer
# composes ONE human-readable line per fact (I-FRAMES-FACTS-ONLY). Defensive:
# unknown / missing fields degrade to a bare label, never crash.

_MARATHON_FACT_TYPES = ("marathon_plan", "milestone", "marathon_paused", "todo")


def stop_report_lines(report) -> list[str]:
    """The kernel's report of a stop, one line per fact, as the kernel sent it (nothing is judged or reworded
    here): what it saw proven, the item the work stands on, each call tried there with how it ended, what waits
    on the person, what is still open. A field that is missing or of another shape is skipped, never an error."""
    r = report if isinstance(report, dict) else {}

    def texts(key):
        return [str(x).strip() for x in (r.get(key) if isinstance(r.get(key), list) else []) if str(x).strip()]
    out = [f"  ✓ proven: {t}" for t in texts("done")]
    item = str(r.get("item") or "").strip()
    if item:
        out.append(f"  ▸ stands on: {item}")
    for t in r.get("tried") if isinstance(r.get("tried"), list) else []:
        if isinstance(t, dict) and str(t.get("call") or "").strip():
            times = f" x{t['times']}" if isinstance(t.get("times"), int) and t["times"] > 1 else ""
            mark = "" if "ok" not in t else ("ran" if t.get("ok") else "FAILED")
            result = str(t.get("result") or "").strip()
            out.append("  · tried: " + " ".join(x for x in (mark + times, f"`{t['call']}`") if x.strip())
                       + (f" -- {result}" if result else ""))
    for w in r.get("waits") if isinstance(r.get("waits"), list) else []:
        if isinstance(w, dict) and str(w.get("on") or "").strip():
            out.append(f"  ? waits on you: {w['on']}" + (f" ({w['item']})" if str(w.get("item") or "").strip() else ""))
    waiting = {str(w.get("item")) for w in (r.get("waits") if isinstance(r.get("waits"), list) else [])
               if isinstance(w, dict)}
    out += [f"  ○ open: {t}" for t in texts("open") if t != item and t not in waiting]
    return out


def marathon_note(frame: dict) -> str:
    """One-line note for a marathon FACT frame (marathon_plan / milestone /
    marathon_paused). Reads common fields with fallbacks so a shape change on
    the kernel side degrades gracefully instead of raising."""
    ftype = frame.get("type", "")
    if ftype == "marathon_plan":
        n = frame.get("milestone_count")
        if n is None and isinstance(frame.get("milestones"), list):
            n = len(frame["milestones"])
        goal = str(frame.get("goal") or frame.get("summary") or "").strip()
        head = f"Marathon plan ({n} milestones)" if n is not None else "Marathon plan"
        return f"🏁 {head}: {goal}".rstrip(": ").rstrip() if goal else f"🏁 {head}"
    if ftype == "milestone":
        label = str(frame.get("title") or frame.get("name") or frame.get("text") or "").strip()
        idx = frame.get("index")
        head = f"Milestone {idx}" if idx is not None else "Milestone"
        status = str(frame.get("status") or ("done" if frame.get("done") else "")).strip()
        tail = f" [{status}]" if status else ""
        return f"• {head}: {label}{tail}".rstrip() if label else f"• {head}{tail}"
    if ftype == "marathon_paused":
        why = str(frame.get("why") or "").strip()
        if why:      # the kernel says why in plain words and reports the stop from its own records
            return "\n".join([f"⏸ Stopped by the kernel: {why}", *stop_report_lines(frame.get("report"))])
        reason = str(frame.get("reason") or frame.get("summary") or "").strip()
        return f"⏸ Marathon paused: {reason}" if reason else "⏸ Marathon paused"
    if ftype == "todo":
        todos = frame.get("todos") if isinstance(frame.get("todos"), list) else []
        total = frame.get("total", len(todos))
        done = frame.get("completed", 0)
        current = next((str(t.get("content", "")) for t in todos
                        if isinstance(t, dict) and t.get("status") == "in_progress"), "")
        head = f"📋 Todos {done}/{total}"
        return f"{head} — now: {current}" if current else head
    return str(frame.get("type", ""))


def render_todo_frame(frame: dict, sink) -> None:
    """Full-checklist render for a `todo` FACT (I-WEBBEE-CODE-TODO-SCRATCHPAD).
    The kernel republishes the WHOLE list on every todo_write, so route it to
    sink.todos(items, total, done) — the sink redraws the complete current
    checklist inline (Claude-Code-style: the latest full list always sits in
    the scrollback). Guarded like the other optional sink hooks: a minimal
    sink without `todos` degrades to the old one-line marathon_note, and a
    render error must never break the frame loop in session.run()."""
    todos = frame.get("todos") if isinstance(frame.get("todos"), list) else []
    total = frame.get("total", len(todos))
    done = frame.get("completed", 0)
    render = getattr(sink, "todos", None)
    try:
        if render is not None:
            render(todos, total, done)
        else:
            note = getattr(sink, "note", None)
            if note is not None:
                note(marathon_note(frame))
    except Exception:
        pass
