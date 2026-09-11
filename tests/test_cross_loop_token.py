import asyncio
import sys
import threading
import types
import pytest
from webbee.session import AgentSession

class FakeCfg:
    api_url = "https://api.imperal.io"

try:
    import imperal_mcp.client
except ImportError:
    if "imperal_mcp" not in sys.modules:
        pkg = types.ModuleType("imperal_mcp")
        sys.modules["imperal_mcp"] = pkg
    if "imperal_mcp.client" not in sys.modules:
        m = types.ModuleType("imperal_mcp.client")
        class FakeImperalClient:
            def __init__(self, cfg, token_provider=None):
                self.cfg = cfg
                self._token_provider = token_provider
        m.ImperalClient = FakeImperalClient
        sys.modules["imperal_mcp.client"] = m
        sys.modules["imperal_mcp"].client = m

@pytest.mark.asyncio
async def test_bridge_cross_loop():
    main_loop = asyncio.get_running_loop()
    main_lock = asyncio.Lock()
    await main_lock.acquire()

    async def mock_token_provider():
        async with main_lock:
            return "jwt-token-verified"

    session = AgentSession(FakeCfg(), mock_token_provider, ".")
    factory = session._file_client_factory()
    assert factory is not None

    def worker():
        async def work():
            client = factory()
            token = await client._token_provider()
            return token
        return asyncio.run(work())

    t = threading.Thread(target=worker)
    t.start()
    await asyncio.sleep(0.05)
    main_lock.release()
    while t.is_alive():
        await asyncio.sleep(0.01)
    t.join()
