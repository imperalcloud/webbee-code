import os
import subprocess


def handle_tool_request(frame: dict, executor, gate, prompt) -> dict:
    """Offline-testable core of the tool loop. `frame` is a `tool_request`
    frame; `executor` is a LocalToolExecutor; `gate` is a ConsentGate;
    `prompt` is a callable (tool, args) -> bool (True = allow)."""
    req_id = frame["req_id"]
    tool = frame["tool"]
    args = frame.get("args", {})

    d = gate.evaluate(tool)
    if not d.allow:
        result = {"ok": False, "content": d.reason}
    elif d.needs_prompt and not prompt(tool, args):
        result = {"ok": False, "content": "user declined"}
    else:
        result = executor.run(tool, args)

    return {"req_id": req_id, "result": result}


def build_coding_context(workspace_root: str) -> dict:
    """Snapshot of the workspace to hand the cloud brain: cwd (realpath),
    `git status -sb` output (empty string for non-git dirs / any error),
    and a bounded newline-joined file tree."""
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
            rel = os.path.relpath(os.path.join(dirpath, fn), cwd)
            paths.append(rel)
            if len(paths) >= 200:
                break
        if len(paths) >= 200:
            break
    tree = "\n".join(paths)

    return {"cwd": cwd, "git": git, "tree": tree}


class AgentSession:
    """Client-side driver for a coding session against the Imperal cloud.
    The brain runs server-side; this is the "hands" — it streams down
    tool_request frames over SSE, runs each tool locally under the
    ConsentGate, and posts results back until a final frame arrives."""

    def __init__(self, cfg, token_provider, workspace_root: str, mode: str = "default") -> None:
        self.cfg = cfg
        self.token_provider = token_provider
        self.workspace_root = workspace_root
        self.mode = mode

    async def _headers(self) -> dict:
        token = await self.token_provider()
        return {"Authorization": f"Bearer {token}"}

    def _prompt(self, tool: str, args: dict) -> bool:
        resp = input(f"Run {tool} {args}? [y/N] ")
        return resp.strip().lower() == "y"

    async def run(self, task: str) -> str:
        import httpx
        from httpx_sse import aconnect_sse

        from webbee.consent import ConsentGate
        from webbee.tools import LocalToolExecutor
        from imperal_mcp.client import ImperalClient

        coding_context = build_coding_context(self.workspace_root)
        imperal_id = await ImperalClient(self.cfg, self.token_provider).whoami()

        executor = LocalToolExecutor(self.workspace_root)
        gate = ConsentGate(self.mode)

        headers = await self._headers()
        async with httpx.AsyncClient(base_url=self.cfg.api_url, timeout=60) as client:
            resp = await client.post(
                "/v1/agent/sessions",
                json={"user_id": imperal_id, "task": task, "coding_context": coding_context},
                headers=headers,
            )
            resp.raise_for_status()
            session_id = resp.json()["session_id"]

            headers = await self._headers()
            async with aconnect_sse(
                client, "GET", f"/v1/agent/sessions/{session_id}/stream", headers=headers,
            ) as event_source:
                async for sse in event_source.aiter_sse():
                    frame = sse.json()
                    if frame.get("type") == "tool_request":
                        out = handle_tool_request(frame, executor, gate, self._prompt)
                        headers = await self._headers()
                        await client.post(
                            f"/v1/agent/sessions/{session_id}/result",
                            json=out,
                            headers=headers,
                        )
                    elif frame.get("type") == "final":
                        return frame["text"]

        return ""
