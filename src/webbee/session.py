import asyncio
import os
import subprocess

from webbee.frames import (
    _first_time,
    _progress_text,
    handle_action_frame,
    handle_step_finished,
    handle_step_started,
)


def handle_tool_request(frame: dict, executor) -> dict:
    """Run a (kernel-pre-approved) local tool. The kernel gates write/bash
    consent via confirm_request BEFORE dispatching, so a tool_request here is
    always cleared to run."""
    return {"req_id": frame["req_id"], "result": executor.run(frame["tool"], frame.get("args", {}))}


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
    raw = await ask_consent(frame.get("app_id", ""), frame.get("tool", ""), frame.get("args", {}))
    return {"req_id": req_id, "result": {"consent_reply": raw}}


def build_coding_context(workspace_root: str) -> dict:
    """Snapshot handed to the cloud brain: cwd (realpath), `git status -sb`
    (empty for non-git/any error), and a bounded newline-joined file tree."""
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
    return {"cwd": cwd, "git": git, "tree": "\n".join(paths)}


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

    def __init__(self, cfg, token_provider, workspace_root: str, mode: str = "default") -> None:
        self.cfg = cfg
        self.token_provider = token_provider
        self.workspace_root = workspace_root
        self.mode = mode
        self.session_id: str = ""
        self.steps: list = []

    async def _headers(self) -> dict:
        token = await self.token_provider()
        return {"Authorization": f"Bearer {token}"}

    async def run(self, task: str, sink) -> str:
        import httpx
        from httpx_sse import aconnect_sse

        from webbee.tools import LocalToolExecutor
        from imperal_mcp.client import ImperalClient

        # Offload to a worker thread — build_coding_context runs sync
        # subprocess.run(git status, timeout=10) + os.walk; inline on the dock's
        # asyncio loop it froze the whole UI at every turn start.
        coding_context = await asyncio.to_thread(build_coding_context, self.workspace_root)
        imperal_id = await ImperalClient(self.cfg, self.token_provider).whoami()
        executor = LocalToolExecutor(self.workspace_root)

        headers = await self._headers()
        async with httpx.AsyncClient(base_url=self.cfg.api_url, timeout=60) as client:
            resp = await client.post(
                "/v1/agent/sessions",
                json={"user_id": imperal_id, "task": task, "coding_context": coding_context},
                headers=headers,
            )
            resp.raise_for_status()
            session_id = resp.json()["session_id"]
            self.session_id = session_id
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
            headers = await self._headers()
            async with aconnect_sse(
                client, "GET", f"/v1/agent/sessions/{session_id}/stream", headers=headers,
            ) as event_source:
                async for sse in event_source.aiter_sse():
                    frame = sse.json()
                    ftype = frame.get("type")

                    if ftype == "tool_request":
                        rid = frame.get("req_id")
                        sid = str(rid or "")
                        if rid in seen:
                            out = seen[rid]
                        else:
                            if _first_time(sid, started):
                                sink.tool_start(frame.get("tool", ""), frame.get("args", {}))
                            out = handle_tool_request(frame, executor)
                            res = out["result"]
                            if _first_time(sid, finished):
                                sink.tool_result(frame.get("tool", ""), bool(res.get("ok")), _summary(res))
                                self.steps.append({"step_id": sid,
                                                   "label": frame.get("tool", ""),
                                                   "ok": bool(res.get("ok"))})
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

                    elif ftype == "progress":  # P2 — dual-reads llm_text (v2) / text (legacy)
                        sink.progress(_progress_text(frame))

                    elif ftype == "usage":  # P2 — cumulative tokens + credits (Slice C; raw $ stays server-side)
                        sink.usage(
                            int(frame.get("tokens", 0) or 0),
                            int(frame.get("credits", 0) or 0),
                        )

                    elif ftype == "final":
                        return frame.get("text", "")

        return ""

    async def _post_result(self, client, session_id: str, out: dict) -> None:
        headers = await self._headers()
        await client.post(f"/v1/agent/sessions/{session_id}/result", json=out, headers=headers)

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
