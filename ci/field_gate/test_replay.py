"""A transcript turns into the hook inputs Claude Code sent, in order; scrubbing removes secrets and nothing else."""
import json

from ci.field_gate.replay import scrub, transcript_events


def _write(tmp_path, records):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def test_replay_roundtrip(tmp_path):
    base = {"sessionId": "s1", "cwd": "/w"}
    path = _write(tmp_path, [
        {**base, "type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": "check it"}},
        {**base, "type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}], "stop_reason": "tool_use"}},
        {**base, "type": "user", "toolUseResult": {"stdout": "a.py", "stderr": ""},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]}},
        {**base, "type": "assistant", "message": {"content": [{"type": "text", "text": "a.py is there"}],
                                                  "stop_reason": "end_turn"}},
        {**base, "type": "user", "message": {"content": [{"type": "text", "text": "Stop hook feedback:\nKERNEL FACTS"}]}},
    ])
    ev = transcript_events(path)
    assert [e["event"] for e in ev] == ["prompt", "post-tool", "stop", "recorded_block"]
    assert ev[1]["payload"]["tool_response"] == {"stdout": "a.py", "stderr": ""}
    assert ev[2]["payload"]["last_assistant_message"] == "a.py is there"


def test_scrub_removes_secrets_only():
    ev = [{"event": "post-tool", "payload": {"tool_response": "origin https://gate:ghp_FAKEgate0000@x.invalid ok"}}]
    out = scrub(ev, ["ghp_FAKEgate0000"])
    text = out[0]["payload"]["tool_response"]
    assert "ghp_FAKEgate0000" not in text and text.endswith("ok") and "origin" in text
