"""Regression: a tool/consent error must NEVER hang the turn (frozen dock).

The kernel waits for a result on tool_request / confirm_request; an UNPOSTED
result — a re-raised OutsideWorkspaceError, a crashing executor, or a failed
consent UI — hung the kernel dispatch and froze the whole dock (had to close the
terminal). Every path must return a result so the kernel unblocks."""
import asyncio

from webbee.tools import LocalToolExecutor
from webbee.session import handle_tool_request, handle_confirm_request


def test_outside_workspace_returns_result_not_raise(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    # A path outside the workspace ("..") must yield a graceful ok:False result,
    # NEVER raise — a re-raise escaped run(), the result was never posted, and
    # the kernel hung waiting (frozen dock).
    r = ex.run("read_file", {"path": "../../etc/hosts"})
    assert isinstance(r, dict) and r["ok"] is False
    assert "outside the workspace" in r["content"].lower()


def test_run_never_raises_on_bad_args(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    # a garbage tool / missing args returns a result dict, never raises
    assert ex.run("does_not_exist", {})["ok"] is False
    assert isinstance(ex.run("read_file", {}), dict)  # missing 'path' -> result, not crash


def test_handle_tool_request_never_raises_on_executor_crash():
    class _Boom:
        def run(self, tool, args):
            raise RuntimeError("kaboom")
    out = handle_tool_request({"req_id": "r1", "tool": "x", "args": {}}, _Boom())
    assert out["req_id"] == "r1"
    assert out["result"]["ok"] is False
    assert "kaboom" in out["result"]["content"]


def test_confirm_request_consent_error_declines():
    async def _boom_consent(app_id, tool, args):
        raise RuntimeError("consent ui gone")
    out = asyncio.run(handle_confirm_request(
        {"req_id": "c1", "tool": "write_file"}, "default", _boom_consent))
    assert out["req_id"] == "c1"
    # safe-by-default: decline, and CRUCIALLY a result is returned (no hang)
    assert out["result"]["approved"] is False
    assert out["result"]["reason"] == "consent_error"
