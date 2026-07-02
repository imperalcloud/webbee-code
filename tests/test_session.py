import os
import pytest
from webbee.session import handle_tool_request, handle_confirm_request, build_coding_context
from webbee.tools import LocalToolExecutor
from webbee.consent import ConsentGate


def _ex_gate(tmp_path, mode):
    return LocalToolExecutor(str(tmp_path)), ConsentGate(mode)


def test_read_autoexecutes_no_prompt(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    ex, gate = _ex_gate(tmp_path, "autopilot")
    prompted = []
    out = handle_tool_request(
        {"type": "tool_request", "req_id": "r1", "tool": "read_file", "args": {"path": "a.txt"}},
        ex, gate, prompt=lambda *a: prompted.append(a) or True,
    )
    assert out["req_id"] == "r1"
    assert out["result"]["ok"] and out["result"]["content"] == "hi"
    assert prompted == []  # reads never prompt


def test_plan_mode_refuses_write(tmp_path):
    ex, gate = _ex_gate(tmp_path, "plan")
    out = handle_tool_request(
        {"type": "tool_request", "req_id": "r2", "tool": "write_file", "args": {"path": "a", "content": "x"}},
        ex, gate, prompt=lambda *a: True,
    )
    assert not out["result"]["ok"] and "plan mode" in out["result"]["content"]


def test_default_declined_when_prompt_false(tmp_path):
    ex, gate = _ex_gate(tmp_path, "default")
    out = handle_tool_request(
        {"type": "tool_request", "req_id": "r3", "tool": "bash", "args": {"command": "ls"}},
        ex, gate, prompt=lambda *a: False,
    )
    assert not out["result"]["ok"] and "declined" in out["result"]["content"]


def test_default_runs_when_prompt_true(tmp_path):
    (tmp_path / "x.txt").write_text("1")
    ex, gate = _ex_gate(tmp_path, "default")
    out = handle_tool_request(
        {"type": "tool_request", "req_id": "r4", "tool": "bash", "args": {"command": "ls"}},
        ex, gate, prompt=lambda *a: True,
    )
    assert out["result"]["ok"] and "x.txt" in out["result"]["content"]


def test_build_coding_context_keys(tmp_path):
    (tmp_path / "f.py").write_text("x=1\n")
    ctx = build_coding_context(str(tmp_path))
    assert ctx["cwd"] == os.path.realpath(str(tmp_path))
    assert "f.py" in ctx["tree"]
    assert "git" in ctx  # "" for a non-git dir is fine


def test_confirm_autopilot_auto_approves():
    out = handle_confirm_request(
        {"type": "confirm_request", "req_id": "c1", "app_id": "notes",
         "tool": "delete_note", "args": {}}, prompt=lambda *a: False, mode="autopilot")
    assert out == {"req_id": "c1", "result": {"approved": True}}


def test_confirm_default_prompts_and_declines():
    out = handle_confirm_request(
        {"req_id": "c2", "app_id": "notes", "tool": "delete_note", "args": {}},
        prompt=lambda *a: False, mode="default")
    assert out["result"]["approved"] is False


def test_confirm_default_prompts_and_approves():
    out = handle_confirm_request(
        {"req_id": "c3", "app_id": "notes", "tool": "create_note", "args": {"t": "x"}},
        prompt=lambda *a: True, mode="default")
    assert out["result"]["approved"] is True


def test_confirm_plan_disables_writes():
    out = handle_confirm_request(
        {"req_id": "c4", "app_id": "notes", "tool": "create_note", "args": {}},
        prompt=lambda *a: True, mode="plan")
    assert out["result"]["approved"] is False
