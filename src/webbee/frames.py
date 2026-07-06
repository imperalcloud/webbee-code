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


def _summary_from_facts(facts: dict) -> str:
    """Renderer-composed one-line summary from v2 structured summary_facts
    (I-FRAMES-FACTS-ONLY: the kernel emits facts, never prose here). Degrades
    gracefully to a bare count when entity_kind is empty -- today's kernel
    call sites don't set it yet (Slice-5 T8)."""
    facts = facts or {}
    count = facts.get("count")
    if count is None:
        return ""
    kind = str(facts.get("entity_kind") or "").strip()
    if not kind:
        return str(count)
    return f"{count} {kind}" if count == 1 else f"{count} {kind}s"


def _progress_text(frame: dict) -> str:
    """Dual-read: v2 progress frames carry BOTH 'llm_text' (canonical) and
    'text' (legacy) during the compat window; a v1-only frame carries only
    'text'. Prefer llm_text when present."""
    return str(frame.get("llm_text") or frame.get("text") or "")


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


def handle_step_started(frame: dict, sink, started: set, step_labels: dict, local_ids: set) -> None:
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
    if _first_time(sid, started):
        sink.tool_start(label, {})


def handle_step_finished(frame: dict, sink, finished: set, step_labels: dict, steps: list,
                         local_ids: set) -> None:
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
    sink.tool_result(label, ok, summary)
    steps.append({"step_id": sid, "label": label, "ok": ok})


def handle_action_frame(frame: dict, sink, started: set, finished: set, steps: list) -> None:
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
    sink.tool_result(lbl, ok, summ)
    steps.append({"step_id": sid, "label": lbl, "ok": ok})
