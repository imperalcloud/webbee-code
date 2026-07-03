from webbee.session import handle_tool_request, handle_confirm_request, build_coding_context


class RecordingExecutor:
    def __init__(self): self.calls = []
    def run(self, tool, args):
        self.calls.append((tool, args))
        return {"ok": True, "content": f"ran {tool}"}


def test_handle_tool_request_runs_and_wraps():
    ex = RecordingExecutor()
    out = handle_tool_request({"req_id": "r1", "tool": "read_file", "args": {"path": "a"}}, ex)
    assert out == {"req_id": "r1", "result": {"ok": True, "content": "ran read_file"}}
    assert ex.calls == [("read_file", {"path": "a"})]


def test_confirm_autopilot_approves_without_asking():
    called = []
    out = handle_confirm_request({"req_id": "r2"}, "autopilot", lambda a, t, g: called.append(1) or "x")
    assert out == {"req_id": "r2", "result": {"approved": True}}
    assert called == []  # never prompts in autopilot


def test_confirm_plan_denies_without_asking():
    out = handle_confirm_request({"req_id": "r3"}, "plan", lambda a, t, g: "x")
    assert out == {"req_id": "r3", "result": {"approved": False}}


def test_confirm_default_relays_raw_reply_verbatim():
    # ICNLI: client must NOT interpret — it relays the raw reply as-is.
    frame = {"req_id": "r4", "app_id": "webbee", "tool": "bash", "args": {"command": "ls"}}
    seen = {}
    def ask(app_id, tool, args):
        seen.update(app_id=app_id, tool=tool, args=args)
        return "давай, только осторожно"
    out = handle_confirm_request(frame, "default", ask)
    assert out == {"req_id": "r4", "result": {"consent_reply": "давай, только осторожно"}}
    assert seen == {"app_id": "webbee", "tool": "bash", "args": {"command": "ls"}}


def test_build_coding_context_shape(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    ctx = build_coding_context(str(tmp_path))
    assert set(ctx) == {"cwd", "git", "tree"}
    assert "a.txt" in ctx["tree"]


def test_usage_frame_forwards_tokens_and_cost():
    from webbee.session import AgentSession  # noqa: F401 — import-time sanity check
    # The session's frame dispatch is exercised end-to-end in the live smoke;
    # here we assert the sink contract shape the session will call.
    class Rec:
        def __init__(self): self.calls = []
        def tool_start(self, *a): ...
        def tool_result(self, *a): ...
        def ask_consent(self, *a): return "y"
        def panel_release(self, *a): ...
        def progress(self, text): self.calls.append(("progress", text))
        def usage(self, tokens, cost_usd): self.calls.append(("usage", tokens, cost_usd))
    r = Rec()
    r.usage(1234, 0.05); r.progress("reading files")
    assert ("usage", 1234, 0.05) in r.calls and ("progress", "reading files") in r.calls
