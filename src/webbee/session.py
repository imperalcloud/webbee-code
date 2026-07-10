import asyncio
import os
import subprocess

from webbee.frames import (
    _MARATHON_FACT_TYPES,
    _first_time,
    _progress_text,
    handle_action_frame,
    handle_step_finished,
    handle_step_started,
    marathon_note,
)


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


async def handle_confirm_request(frame: dict, mode: str, ask_consent) -> dict:
    """ICNLI consent path. The client does NOT interpret consent words — it
    relays the user's RAW reply; the kernel brain interprets intent
    (safe-by-default). autopilot/plan are explicit and never prompt.
    `ask_consent(app_id, tool, args)` is an async coroutine returning the raw
    reply (resolved through the pinned dock, or a sync fallback reader)."""
    req_id = frame["req_id"]
    if mode == "autopilot":
        return {"req_id": req_id, "result": {"approved": True}}
    if mode == "plan":
        return {"req_id": req_id, "result": {"approved": False, "reason": "plan_mode"}}
    try:
        raw = await ask_consent(frame.get("app_id", ""), frame.get("tool", ""), frame.get("args", {}))
    except Exception:
        # Consent UI failed -> decline (safe-by-default). NEVER leave the kernel
        # hanging on an unanswered confirm — an unposted result froze the dock.
        return {"req_id": req_id, "result": {"approved": False, "reason": "consent_error"}}
    return {"req_id": req_id, "result": {"consent_reply": raw}}


def build_coding_context(workspace_root: str, intel=None) -> dict:
    """Snapshot handed to the cloud brain: cwd (realpath), `git status -sb`
    (empty for non-git/any error), a bounded newline-joined file tree, and —
    when a ready intel service is injected — the precomputed repo_profile.
    The profile is READ from the already-built index (cheap); indexing never
    happens inline here."""
    cwd = os.path.realpath(workspace_root)
    try:
        proc = subprocess.run(
            ["git", "status", "-sb"], cwd=cwd,
            capture_output=True, text=True, timeout=10,
        )
        git = proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        git = ""
    paths = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d != ".git" and not d.startswith(".")]
        for fn in filenames:
            paths.append(os.path.relpath(os.path.join(dirpath, fn), cwd))
            if len(paths) >= 200:
                break
        if len(paths) >= 200:
            break
    from webbee.repo import compute_repo_key, find_repo_root
    root = find_repo_root(cwd)
    d = {"cwd": cwd, "git": git, "tree": "\n".join(paths),
         "repo_key": compute_repo_key(root), "repo_root": root}
    if intel is not None and getattr(intel, "ready", False):
        try:
            d["repo_profile"] = intel.repo_profile()
        except Exception:
            pass
    return d


def detect_verify_cmd(repo_root: str) -> str:
    """Best-effort project test command, CLIENT-detected from the repo layout.

    SECURITY-load-bearing: in a marathon the kernel runs ONLY this command as
    proof-of-done — the cloud brain can never author a shell command. So this
    is deliberately small + honest: a fixed command per recognised ecosystem,
    NO guessing beyond these. An empty string means "no known runner" → the
    kernel falls back to an LLM-judged done-check. Checked in a stable order."""
    import json as _json

    root = repo_root or "."

    def _has(name: str) -> bool:
        return os.path.isfile(os.path.join(root, name))

    if _has("pyproject.toml") or _has("setup.cfg") or _has("tox.ini"):
        return "pytest -q"
    if _has("package.json"):
        try:
            with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
                pkg = _json.load(f)
            scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
            if isinstance(scripts, dict) and scripts.get("test"):
                return "npm test"
        except (OSError, ValueError):
            pass
    if _has("Cargo.toml"):
        return "cargo test"
    if _has("go.mod"):
        return "go test ./..."
    if _has("Makefile"):
        try:
            import re as _re
            with open(os.path.join(root, "Makefile"), encoding="utf-8") as f:
                if _re.search(r"(?m)^test:", f.read()):
                    return "make test"
        except OSError:
            pass
    return ""


def _summary(result: dict) -> str:
    """One-line summary of a tool result for the UI."""
    content = str(result.get("content", ""))
    first = content.strip().splitlines()[0] if content.strip() else ""
    return first[:120]


class AgentSession:
    """Client-side driver for one coding turn against the Imperal cloud.
    The brain runs server-side; this is the hands — it streams kernel-
    pre-approved tool_request frames over SSE, runs each tool locally, relays
    confirm_request replies RAW for the brain to interpret, drives the sink
    for live UI, and posts results back until a final frame arrives.

    P1: one POST per turn (server reloads the shared webbee-terminal thread,
    so context carries across turns). Persistent signal-based sessions are P3."""

    def __init__(self, cfg, token_provider, workspace_root: str, mode: str = "default", intel=None) -> None:
        self.cfg = cfg
        self.token_provider = token_provider
        self.workspace_root = workspace_root
        self.mode = mode
        self.session_id: str = ""
        self.steps: list = []
        self._task_id: str = ""
        self._intel = intel  # IntelService, or None (base install / boot failure)

    async def _headers(self) -> dict:
        token = await self.token_provider()
        return {"Authorization": f"Bearer {token}"}

    async def run(self, task: str, sink, *, marathon: bool = False, goal: str = "") -> str:
        import httpx

        from webbee.tools import LocalToolExecutor
        from imperal_mcp.client import ImperalClient

        # Offload to a worker thread — build_coding_context runs sync
        # subprocess.run(git status, timeout=10) + os.walk; inline on the dock's
        # asyncio loop it froze the whole UI at every turn start.
        coding_context = await asyncio.to_thread(build_coding_context, self.workspace_root, self._intel)
        if marathon:
            # verify_cmd is CLIENT-detected here and carried in coding_context —
            # the trusted proof-of-done the kernel runs (never brain-authored).
            root = coding_context.get("repo_root") or coding_context.get("cwd") or self.workspace_root
            verify_cmd = await asyncio.to_thread(detect_verify_cmd, root)
            coding_context = {**coding_context, "verify_cmd": verify_cmd}
        imperal_id = await ImperalClient(self.cfg, self.token_provider).whoami()
        executor = LocalToolExecutor(self.workspace_root, indexer=self._intel)

        body = {"user_id": imperal_id, "task": task, "coding_context": coding_context}
        if marathon:
            body["marathon"] = True
            body["goal"] = goal

        headers = await self._headers()
        async with httpx.AsyncClient(base_url=self.cfg.api_url, timeout=60) as client:
            resp = await client.post(
                "/v1/agent/sessions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            _sess = resp.json()
            session_id = _sess["session_id"]
            start_id = _sess.get("last_id", "0-0")
            self.session_id = session_id
            self._task_id = _sess.get("task_id", "")
            self.steps = []

            seen: dict = {}  # req_id -> already-posted result (at-least-once dedup)
            # Slice-5 T9: id-sets shared across BOTH vocabularies for EXT tools
            # (the kernel reuses the SAME tc["id"] as step_id there), so a step
            # announced by one vocabulary is never re-announced by its
            # dual-emitted twin. step_labels carries a v2 step_started's label
            # forward to its later step_finished (which carries no app_id/tool
            # of its own — facts-only). local_ids tracks LOCAL-tool v2 step_ids,
            # which use a disjoint id space from the tool_request round trip
            # (see webbee.frames' module docstring) and are a pure no-op.
            started: set = set()
            finished: set = set()
            step_labels: dict = {}
            local_ids: set = set()
            from webbee.stream import stream_frames
            async for frame in stream_frames(
                client, session_id, self._headers, start_id=start_id,
            ):
                ftype = frame.get("type")

                _ftid = frame.get("task_id", "")
                # Ignore actionable frames from a DIFFERENT turn on the shared
                # persistent stream (task_id absent on legacy kernels -> not filtered).
                if _ftid and self._task_id and _ftid != self._task_id and ftype in (
                        "tool_request", "confirm_request", "final",
                        "marathon_complete", "panel_release_required"):
                    continue

                if ftype == "tool_request":
                    rid = frame.get("req_id")
                    sid = str(rid or "")
                    if rid in seen:
                        out = seen[rid]
                    else:
                        # UI rendering is guarded so it can never block the
                        # result POST below (an unposted result hangs the kernel
                        # dispatch and freezes the dock).
                        if _first_time(sid, started):
                            try:
                                sink.tool_start(frame.get("tool", ""), frame.get("args", {}))
                            except Exception:
                                pass
                        out = await asyncio.to_thread(handle_tool_request, frame, executor)
                        res = out["result"]
                        if _first_time(sid, finished):
                            try:
                                sink.tool_result(frame.get("tool", ""), bool(res.get("ok")), _summary(res))
                                self.steps.append({"step_id": sid,
                                                   "label": frame.get("tool", ""),
                                                   "ok": bool(res.get("ok"))})
                            except Exception:
                                pass
                        seen[rid] = out
                    await self._post_result(client, session_id, out)

                elif ftype == "confirm_request":
                    rid = frame.get("req_id")
                    if rid in seen:
                        out = seen[rid]
                    else:
                        if self.mode == "plan":
                            sink.plan_blocked(frame.get("tool", ""))
                        out = await handle_confirm_request(frame, self.mode, sink.ask_consent)
                        seen[rid] = out
                    await self._post_result(client, session_id, out)

                elif ftype == "panel_release_required":
                    sink.panel_release(frame.get("panel_url", ""), frame.get("summary", ""))

                elif ftype == "action":  # R2 — ext-tool call (server-side) surfaced in the feed
                    handle_action_frame(frame, sink, started, finished, self.steps)

                elif ftype == "step_started":  # v2 (Slice-5 T8 dual-emit)
                    handle_step_started(frame, sink, started, step_labels, local_ids)

                elif ftype == "step_finished":  # v2 (Slice-5 T8 dual-emit)
                    handle_step_finished(frame, sink, finished, step_labels, self.steps, local_ids)

                elif ftype == "thinking":  # system-driven reasoning -> the 💭 block
                    (getattr(sink, "thinking", None) or sink.progress)(_progress_text(frame))

                elif ftype == "progress":  # P2 — dual-reads llm_text (v2) / text (legacy)
                    sink.progress(_progress_text(frame))

                elif ftype == "usage":  # P2 — cumulative tokens + credits (Slice C; raw $ stays server-side)
                    sink.usage(
                        int(frame.get("tokens", 0) or 0),
                        int(frame.get("credits", 0) or 0),
                    )

                elif ftype == "marathon_complete":  # U4 — the whole GOAL is done: terminal
                    return frame.get("text", "")

                elif ftype in _MARATHON_FACT_TYPES:  # U4 — marathon plan/milestone/pause
                    # Facts-only; render ONE human-readable line. Guarded: a
                    # minimal sink (no `note`) simply drops the fact rather than
                    # crashing the turn (the stream reader already tolerates
                    # unknown frame types by ignoring them).
                    _note = getattr(sink, "note", None)
                    if _note is not None:
                        _note(marathon_note(frame))
                    if ftype == "marathon_paused":
                        # Parked (out-of-credits / consent / runaway) -> end the
                        # turn so the dock leaves "working"; the note shows why, and
                        # the run resumes on the user's next reply.
                        return ""

                elif ftype == "final":
                    # In a MARATHON a `final` is a PER-MILESTONE result, NOT the end
                    # of the run -> keep streaming (the goal ends on marathon_complete
                    # / marathon_paused, or a user-stop `final` with stopped=true). A
                    # non-marathon coding turn's `final` is terminal (unchanged).
                    if marathon and not frame.get("stopped"):
                        continue
                    return frame.get("text", "")

        return ""

    async def _post_result(self, client, session_id: str, out: dict) -> None:
        # Best-effort: a result POST must NEVER raise into the stream loop — that
        # would abort the turn and leave the kernel's dispatch hanging (frozen
        # dock). On a transient failure the kernel's tool-wait timeout recovers.
        try:
            headers = await self._headers()
            await client.post(f"/v1/agent/sessions/{session_id}/result", json=out, headers=headers)
        except Exception:
            pass

    async def stop(self) -> None:
        """P5g: server-side stop for Esc/Ctrl-C. Posts a cancel for the
        in-flight turn so the kernel tears down server-side (not just the
        local asyncio task). Fail-soft — the local task.cancel() the dock
        already does is what actually tears the UI down, so a network error
        here must never raise into the key-binding handler."""
        if not self.session_id:
            return
        try:
            import httpx

            headers = await self._headers()
            async with httpx.AsyncClient(base_url=self.cfg.api_url, timeout=10) as client:
                await client.post(
                    f"/v1/agent/sessions/{self.session_id}/cancel", headers=headers,
                )
        except Exception:
            pass
