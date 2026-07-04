import asyncio

from webbee.details import build_step_ref, fetch_step_detail, format_steps


def test_build_step_ref():
    assert build_step_ref("coding-usr-1-123", "toolu_a") == "terminal:coding-usr-1-123:toolu_a"


def test_format_steps_numbers_and_marks():
    steps = [
        {"step_id": "r1", "label": "read_file", "ok": True},
        {"step_id": "a1", "label": "mail·list_messages", "ok": False},
    ]
    out = format_steps(steps)
    assert "1." in out and "2." in out
    assert "read_file" in out and "mail·list_messages" in out
    assert "✓" in out and "✗" in out


def test_format_steps_empty():
    assert "No steps" in format_steps([])


def test_fetch_step_detail_best_effort_on_error():
    class _Cfg:
        api_url = "http://127.0.0.1:1"  # nothing listens — network error path

    async def token_provider():
        return "tok"

    out = asyncio.run(fetch_step_detail(_Cfg(), token_provider, "terminal:s:x"))
    assert out == {}


from webbee.commands import CommandContext, dispatch


def _ctx():
    return CommandContext(mode="default", workspace="/w", version="0.1.9",
                          surface="terminal", logged_in=True,
                          session_tokens=0, session_cost=0.0, git_branch="-")


def test_slash_steps_lists():
    res = dispatch("/steps", _ctx())
    assert res.handled and res.action == "steps"


def test_slash_steps_n_expands():
    res = dispatch("/steps 2", _ctx())
    assert res.handled and res.action == "step_detail" and res.arg == "2"
