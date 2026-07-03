import os
import pytest
from webbee.session import handle_tool_request, handle_confirm_request, build_coding_context
from webbee.tools import LocalToolExecutor


def test_tool_request_reads(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    ex = LocalToolExecutor(str(tmp_path))
    out = handle_tool_request(
        {"type": "tool_request", "req_id": "r1", "tool": "read_file", "args": {"path": "a.txt"}},
        ex,
    )
    assert out["req_id"] == "r1"
    assert out["result"]["ok"] and out["result"]["content"] == "hi"


def test_tool_request_runs_write_pre_approved(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    out = handle_tool_request(
        {"type": "tool_request", "req_id": "r2", "tool": "write_file",
         "args": {"path": "a", "content": "x"}},
        ex,
    )
    assert out["req_id"] == "r2"
    assert out["result"]["ok"]
    assert (tmp_path / "a").read_text() == "x"


def test_tool_request_runs_bash_pre_approved(tmp_path):
    (tmp_path / "x.txt").write_text("1")
    ex = LocalToolExecutor(str(tmp_path))
    out = handle_tool_request(
        {"type": "tool_request", "req_id": "r3", "tool": "bash", "args": {"command": "ls"}},
        ex,
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
         "tool": "delete_note", "args": {}}, mode="autopilot", read_reply=lambda *_: "should not be called")
    assert out == {"req_id": "c1", "result": {"approved": True}}


def test_confirm_plan_disables_writes():
    out = handle_confirm_request(
        {"req_id": "c4", "app_id": "notes", "tool": "create_note", "args": {}},
        mode="plan", read_reply=lambda *_: "should not be called")
    assert out == {"req_id": "c4", "result": {"approved": False}}


def test_confirm_default_relays_raw_reply_approve_like():
    out = handle_confirm_request(
        {"req_id": "c2", "app_id": "notes", "tool": "create_note", "args": {}},
        mode="default", read_reply=lambda *_: "давай конечно")
    assert out == {"req_id": "c2", "result": {"consent_reply": "давай конечно"}}


def test_confirm_default_relays_raw_reply_decline_like():
    out = handle_confirm_request(
        {"req_id": "c3", "app_id": "notes", "tool": "delete_note", "args": {}},
        mode="default", read_reply=lambda *_: "нет")
    assert out == {"req_id": "c3", "result": {"consent_reply": "нет"}}
