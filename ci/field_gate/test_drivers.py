"""The drivers reach the INSTALLED products: imperal-mcp over stdio, webbee's executor as its CLI boots it, the hooks."""
import pytest

from ci.field_gate.drivers import McpDriver, WebbeeDriver, hook
from ci.field_gate.fixture import build_fixture


@pytest.fixture
def fx(tmp_path):
    return build_fixture(tmp_path / "ws")


@pytest.mark.asyncio
async def test_mcp_lists_tools(fx, tmp_path):
    async with McpDriver(fx.root, tmp_path / "home-mcp") as mcp:
        names = await mcp.list_tools()
    assert {"outline", "read_symbol", "grep_code", "checkpoint_diff", "state_lens"} <= set(names)


@pytest.mark.asyncio
async def test_mcp_call_returns_text(fx, tmp_path):
    async with McpDriver(fx.root, tmp_path / "home-mcp") as mcp:
        out = await mcp.call("outline", {"path": "core/app/mid.py"})
    assert out["text"]


@pytest.mark.asyncio
async def test_webbee_runs_read_file(fx, tmp_path):
    async with WebbeeDriver(fx.root, tmp_path / "home-wb") as wb:
        out = await wb.run("read_file", {"path": "lib-sdk/lib_sdk/emit.py"})
    assert "class Emitter" in str(out)


def test_hook_session_start_exit0(fx, tmp_path):
    res = hook("session-start", {"hook_event_name": "SessionStart", "session_id": "gate-test", "cwd": str(fx.root)},
               tmp_path / "home-hook")
    assert res["code"] == 0
