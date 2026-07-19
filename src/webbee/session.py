import asyncio

from webbee.coding_context import build_coding_context, detect_verify_cmd
from webbee.consent import _retire, race_consent
from webbee.frames import (
    _MARATHON_FACT_TYPES,
    _first_time,
    _is_foreign_frame,
    _origin_tag,
    _progress_text,
    _summary,
    handle_action_frame,
    handle_step_finished,
    handle_step_started,
    handle_tool_request,
    marathon_note,
    render_foreign_frame,
    render_todo_frame,
)


def _is_transient_status(status: int) -> bool:
    return status >= 500 or status in (408, 429)


async def _transient_retry(send, *, attempts: int = 5, base: float = 1.0, cap: float = 8.0):
    """Bounded transient-retry for gateway WRITE calls (turn-start POST): a
    502 during a deploy must not kill the turn. Verdict/normal statuses return
    immediately; transport errors and 5xx/408/429 retry with capped backoff.
    The LAST failure is returned/raised for the caller's raise_for_status."""
    backoff = base
    last_exc = None
    last_resp = None
    for _ in range(max(1, attempts)):
        try:
            resp = await send()
            if not _is_transient_status(resp.status_code):
                return resp
            last_resp, last_exc = resp, None
        except (Exception,) as e:            # httpx.HTTPError / OSError
            last_exc, last_resp = e, None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, cap)
    if last_exc is not None:
        raise last_exc
    return last_resp


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

    async def run(self, task: str, sink, *, marathon: bool = False, goal: str = "",
                  surface: str = "", steer_iid: str = "") -> str:
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
        if surface:
            # Liveness v2 §B: an idle-steer pickup carries the queued item's
            # origin surface so the kernel adopts it start-path (provenance +
            # [surface] tags). Additive-only -- a typed turn's body is
            # byte-identical to before.
            body["surface"] = surface
        if steer_iid:
            # steer-iid-dedup: a pickup also carries the queued item's dedup id
            # so the kernel's ring can drop an at-least-once twin. Same
            # additive-only contract -- a typed turn has none, key omitted.
            body["steer_iid"] = steer_iid
        if marathon:
            body["marathon"] = True
            body["goal"] = goal

        headers = await self._headers()
        async with httpx.AsyncClient(base_url=self.cfg.api_url, timeout=60) as client:
            resp = await _transient_retry(lambda: client.post(
                "/v1/agent/sessions", json=body, headers=headers))
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
            _fr = getattr(self.token_provider, "force_refresh", None)
            _rc = getattr(sink, "reconnecting", None)
            stream = stream_frames(client, session_id, self._headers, start_id=start_id,
                                   force_refresh=_fr, on_retry=_rc)
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
                            race = await race_consent(frame, sink, stream,
                                                      mode=self.mode, task_id=self._task_id)
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

                    elif ftype in ("task_queued", "task_dequeued"):
                        # Full-queue-layer K1: a follow-up queued into the RUNNING
                        # kernel session shows in the live queue panel the instant
                        # it queues (tagged by origin) and leaves the panel when
                        # the kernel drains it. These frames carry NO task_id
                        # (they belong to the session, not a turn), so the C7
                        # filter never eats them; getattr-guarded like todos/
                        # queued_run — a minimal sink drops them, a render error
                        # never breaks the loop. Terminal-origin rows render TOO
                        # (mid-turn inject, 0.3.15): an injected line never sits
                        # in the LOCAL panel — the kernel's echo is its only row,
                        # and task_dequeued clears it when the turn absorbs it.
                        _origin = str(frame.get("origin", "") or "")
                        _hook = getattr(sink, "remote_queued" if ftype == "task_queued"
                                        else "remote_dequeued", None)
                        if _hook is not None:
                            _iid = str(frame.get("steer_iid", "") or "")
                            try:
                                if ftype == "task_queued":
                                    _hook(_origin, str(frame.get("text", "") or ""), _iid)
                                else:
                                    _hook(_origin, _iid)
                            except Exception:
                                pass

                    elif ftype == "marathon_complete":  # U4 — the whole GOAL is done: terminal
                        return frame.get("text", "")

                    elif ftype in _MARATHON_FACT_TYPES:  # U4 — marathon plan/milestone/pause/todo
                        # Facts-only. A `todo` fact carries the FULL list -> the
                        # dedicated full-checklist render (falls back to the old
                        # one-line note on a minimal sink). The other facts render
                        # ONE human-readable line. Guarded: a minimal sink (no
                        # `note`) simply drops the fact rather than crashing the
                        # turn (the stream reader already tolerates unknown frame
                        # types by ignoring them).
                        if ftype == "todo":
                            render_todo_frame(frame, sink)
                        else:
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

    _result_delays = (0.5, 2.0, 5.0)   # class attr — tests override per instance

    async def _post_result(self, client, session_id: str, out: dict) -> None:
        # Never raises into the stream loop. W1: bounded transient retries —
        # the kernel dedups by req_id, so a duplicate post is safe, and this
        # cuts outage recovery from the kernel's tool-wait expiry (up to
        # 65min for bash) to seconds. Final fallback unchanged: give up
        # silently, kernel re-dispatches the same req_id.
        for i, delay in enumerate((0.0,) + tuple(self._result_delays)):
            if delay:
                await asyncio.sleep(delay)
            try:
                headers = await self._headers()
                r = await client.post(f"/v1/agent/sessions/{session_id}/result",
                                      json=out, headers=headers)
                if r.status_code < 500:
                    return
            except Exception:
                continue

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
