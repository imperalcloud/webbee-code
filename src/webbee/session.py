import asyncio
import os
import subprocess
from dataclasses import dataclass

from webbee.frames import (
    _FOREIGN_ACTIONABLE_TYPES,
    _MARATHON_FACT_TYPES,
    _first_time,
    _origin_tag,
    _progress_text,
    handle_action_frame,
    handle_step_finished,
    handle_step_started,
    marathon_note,
    render_foreign_frame,
)


def _is_foreign_frame(frame: dict, own_task_id: str) -> bool:
    """C7 filter predicate (extracted verbatim from run()'s inline check): a
    frame stamped with a DIFFERENT task_id on the shared persistent stream,
    when it is origin-stamped or actionable, belongs to another turn and is
    DISPLAY-ONLY for this client. task_id absent (legacy kernels) -> own."""
    ftid = frame.get("task_id", "")
    return bool(ftid and own_task_id and ftid != own_task_id and (
        frame.get("origin") or frame.get("type") in _FOREIGN_ACTIONABLE_TYPES))


async def _retire(task) -> None:
    """Cancel + drain a racing task so it never outlives the frame loop
    (liveness A hygiene). Absorbs the loser's outcome — CancelledError, a
    late result, StopAsyncIteration, a transport error — retirement must
    never introduce a new failure path. An externally-delivered cancellation
    of the CALLER (task finished before our cancel landed) still propagates."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise                       # ours was the outer cancel — propagate
    except Exception:
        pass


def _dismiss_consent(sink) -> None:
    """Tell the UI the pending consent prompt was answered on ANOTHER surface
    (liveness A). Guarded like the other optional sink hooks — a minimal sink
    without `consent_dismissed` (or a UI bug) must never break the frame loop."""
    fn = getattr(sink, "consent_dismissed", None)
    if fn is None:
        return
    try:
        fn("↩ answered from another surface")
    except Exception:
        pass


@dataclass
class _ConsentRace:
    """Outcome of racing the local consent prompt against the stream
    (liveness A). Exactly one shape per race:
      * out          — the prompt resolved locally (today's path): POST it; a
                       pulled-ahead __anext__ may ride along in carry_task.
      * carry_frame  — the stream won: the park ended elsewhere; the prompt was
                       cancelled + dismissed, nothing is posted, and the pulled
                       frame is handed back to the loop for normal handling.
      * stream_ended — StopAsyncIteration mid-consent: exit as stream end.
    """
    out: dict | None = None
    carry_task: asyncio.Task | None = None
    carry_frame: dict | None = None
    stream_ended: bool = False


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

    def __init__(self, cfg, token_provider, workspace_root: str, mode: str = "default", intel=None,
                 shadow=None) -> None:
        self.cfg = cfg
        self.token_provider = token_provider
        self.workspace_root = workspace_root
        self.mode = mode
        self.session_id: str = ""
        self.steps: list = []
        self._task_id: str = ""
        self._intel = intel  # IntelService, or None (base install / boot failure)
        self._shadow = shadow  # ShadowGit, or None (git unavailable / boot failure)

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
        executor = LocalToolExecutor(self.workspace_root, indexer=self._intel,
                                     shadow=self._shadow)

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
            stream = stream_frames(client, session_id, self._headers, start_id=start_id)
            # Liveness A: explicit __anext__ pulls (not `async for`) so a
            # pending local consent can RACE the stream. Between iterations at
            # most ONE of carry_task/carry_frame is set — a consent race hands
            # ownership of its pulled-ahead pull back to this loop: a pending
            # task when consent won, an already-pulled frame when the stream
            # won. Everything else is byte-identical to the old async-for.
            carry_task = None    # a still-pending __anext__ task (consent won)
            carry_frame = None   # an already-pulled frame (the stream won)
            try:
                while True:
                    if carry_frame is not None:
                        frame, carry_frame = carry_frame, None
                    else:
                        if carry_task is not None:
                            pull, carry_task = carry_task, None
                        else:
                            pull = asyncio.ensure_future(stream.__anext__())
                        try:
                            frame = await pull
                        except StopAsyncIteration:
                            break
                    ftype = frame.get("type")

                    # A frame from a DIFFERENT turn on the shared persistent stream
                    # (task_id absent on legacy kernels -> treated as own). C7 safety:
                    # foreign actionable frames are NEVER executed/consented and NEVER
                    # end this turn -- but instead of vanishing they (and any origin-
                    # stamped cross-surface display frame, e.g. a Telegram-steered
                    # turn's progress) now render ONE tagged, display-only line.
                    if _is_foreign_frame(frame, self._task_id):
                        render_foreign_frame(frame, sink)
                        continue

                    # Live steer topology: a Telegram/panel-steered turn keeps THIS
                    # client's task_id (the terminal stays the sole executor) with
                    # `origin` stamped -- tag the text renders below; everything
                    # else (execution, dedup, consent, accounting) is unchanged.
                    _tag = _origin_tag(frame)

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
                                    sink.tool_start(_tag + frame.get("tool", ""), frame.get("args", {}))
                                except Exception:
                                    pass
                            out = await asyncio.to_thread(handle_tool_request, frame, executor)
                            res = out["result"]
                            if _first_time(sid, finished):
                                try:
                                    sink.tool_result(_tag + frame.get("tool", ""), bool(res.get("ok")), _summary(res))
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
                            await self._post_result(client, session_id, seen[rid])
                        else:
                            if self.mode == "plan":
                                sink.plan_blocked(frame.get("tool", ""))
                            # Liveness A: the local prompt races the stream so a
                            # consent answered from ANOTHER surface (Telegram
                            # relay) unfreezes this terminal instead of leaving
                            # the dock stuck on `approve? y/n`.
                            race = await self._race_consent(frame, sink, stream)
                            carry_task, carry_frame = race.carry_task, race.carry_frame
                            if race.stream_ended:
                                break
                            if race.out is not None:
                                seen[rid] = race.out
                                await self._post_result(client, session_id, race.out)

                    elif ftype == "panel_release_required":
                        sink.panel_release(frame.get("panel_url", ""), frame.get("summary", ""))

                    elif ftype == "action":  # R2 — ext-tool call (server-side) surfaced in the feed
                        handle_action_frame(frame, sink, started, finished, self.steps)

                    elif ftype == "step_started":  # v2 (Slice-5 T8 dual-emit)
                        handle_step_started(frame, sink, started, step_labels, local_ids)

                    elif ftype == "step_finished":  # v2 (Slice-5 T8 dual-emit)
                        handle_step_finished(frame, sink, finished, step_labels, self.steps, local_ids)

                    elif ftype == "thinking":  # system-driven reasoning -> the 💭 block
                        _text = _progress_text(frame)
                        (getattr(sink, "thinking", None) or sink.progress)(_tag + _text if _text else "")

                    elif ftype == "progress":  # P2 — dual-reads llm_text (v2) / text (legacy)
                        _text = _progress_text(frame)
                        sink.progress(_tag + _text if _text else "")

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
                        _text = frame.get("text", "")
                        return _tag + _text if _text else ""
            finally:
                # Never leak a pulled-ahead __anext__ past the loop (e.g. an
                # exit while a consent race's carry is still pending).
                if carry_task is not None:
                    await _retire(carry_task)

        return ""

    async def _race_consent(self, frame: dict, sink, stream) -> _ConsentRace:
        """LIVE BUG fix (Valentin, 2026-07-15): a consent answered from
        Telegram resolves the kernel park, but the frame loop used to block
        INLINE in handle_confirm_request — the dock froze on `approve? y/n`
        and the rest of the turn never rendered until a local keypress. The
        local prompt now RACES the stream:
          * the prompt resolves first -> POST it (today's path, unchanged);
            the pulled-ahead frame rides back to the loop in carry_task.
          * a re-published confirm_request with the SAME req_id (kernel
            I-MARATHON-USER-WAKE presence re-publish) -> NOT an answer; the
            prompt stays up and both racers keep going.
          * any other OWN-turn frame -> the park ended elsewhere (answered
            from another surface / superseded): cancel the prompt, dismiss the
            dock, POST NOTHING (the kernel accepts only the FIRST result per
            issued req_id), hand the frame back for normal handling.
          * foreign frames (C7) stay display-only and never end the park.
        Consent is never auto-approved here — autopilot/plan resolve instantly
        inside handle_confirm_request exactly as before."""
        rid = frame.get("req_id")
        consent_task = asyncio.ensure_future(
            handle_confirm_request(frame, self.mode, sink.ask_consent))
        pull = None
        try:
            while True:
                if pull is None:
                    pull = asyncio.ensure_future(stream.__anext__())
                await asyncio.wait({consent_task, pull},
                                   return_when=asyncio.FIRST_COMPLETED)
                if consent_task.done():
                    # The local answer wins — even when both racers finished in
                    # the same cycle the pulled frame is NOT lost: the loop owns
                    # carry_task and processes it next.
                    return _ConsentRace(out=consent_task.result(), carry_task=pull)
                nxt_pull, pull = pull, None
                try:
                    nxt = nxt_pull.result()
                except StopAsyncIteration:
                    # Stream ended mid-consent: retire the prompt safely and
                    # exit exactly as today's stream end (no result posted).
                    await _retire(consent_task)
                    return _ConsentRace(stream_ended=True)
                if _is_foreign_frame(nxt, self._task_id):
                    render_foreign_frame(nxt, sink)
                    continue
                if nxt.get("type") == "confirm_request" and nxt.get("req_id") == rid:
                    continue
                await _retire(consent_task)
                _dismiss_consent(sink)
                return _ConsentRace(carry_frame=nxt)
        except BaseException:
            # Hygiene on any unexpected exit (StreamAuthError from the pull,
            # outer cancellation, a consent-task crash): no racer may leak.
            for t in (consent_task, pull):
                if t is not None and not t.done():
                    await _retire(t)
            raise

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
