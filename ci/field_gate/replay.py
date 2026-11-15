"""Replay a recorded Claude Code session through the installed hooks, as Claude Code sent them.

A transcript (~/.claude/projects/<folder>/<session>.jsonl) holds the user's prompts, each tool call with its result
(`toolUseResult` is what Claude Code hands PostToolUse as `tool_response`), the final answers, and every block the Stop
hook gave ("Stop hook feedback: ..." comes back as a user message). The events below are those hook inputs in order;
`recorded_block` marks where the live hook refused, so a replay can be judged against the labels of that session.

Recorded sessions hold private data: they live outside the repository (IMPERAL_GATE_RECORDED), never in it.
"""
from __future__ import annotations

import json
from pathlib import Path

STOP_FEEDBACK = "Stop hook feedback:"


def _blocks(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else [{"type": "text", "text": content}] if content else []


def transcript_events(path: Path) -> list[dict]:
    events: list[dict] = []
    calls: dict[str, tuple[str, dict]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        kind, msg = rec.get("type"), rec.get("message") or {}
        base = {"session_id": rec.get("sessionId", ""), "cwd": rec.get("cwd", "")}
        if kind == "assistant":
            texts = []
            for b in _blocks(msg):
                if b.get("type") == "tool_use":
                    calls[b["id"]] = (b["name"], b.get("input") or {})
                elif b.get("type") == "text":
                    texts.append(b["text"])
            if msg.get("stop_reason") == "end_turn" and texts:
                events.append({"event": "stop", "payload": {**base, "hook_event_name": "Stop",
                                                            "last_assistant_message": "\n".join(texts)}})
        elif kind == "user":
            for b in _blocks(msg):
                if b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    name, inp = calls.pop(b["tool_use_id"])
                    response = rec.get("toolUseResult", b.get("content"))
                    events.append({"event": "post-tool", "payload": {
                        **base, "hook_event_name": "PostToolUse", "tool_name": name, "tool_input": inp,
                        "tool_response": response}})
                elif b.get("type") == "text":
                    text = b.get("text") or ""
                    if text.startswith(STOP_FEEDBACK):
                        events.append({"event": "recorded_block", "text": text})
                    elif rec.get("origin", {}).get("kind") == "human" or rec.get("promptSource") == "typed":
                        events.append({"event": "prompt", "payload": {**base, "hook_event_name": "UserPromptSubmit",
                                                                      "prompt": text}})
    return events


def scrub(events: list[dict], secrets: list[str]) -> list[dict]:
    """Every string through the product's own scrubber, then the named fakes/secrets out -- the rest unchanged."""
    from imperal_sdk.secret_mask import scrub_secrets      # the one owner of secrets (sdk 6.7.0)

    def clean(v):
        if isinstance(v, str):
            out = scrub_secrets(v)
            for s in secrets:
                out = out.replace(s, "«secret»")
            return out
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    return [clean(e) for e in events]
