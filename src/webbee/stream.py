"""Reconnecting SSE frame iterator (U0). The gateway serves the durable
down-stream with SSE ids; on any transport drop we reconnect and resume from
the last seen id — no frame is ever lost (server keeps 24h of stream). The
caller breaks the async-for on its terminal frame (final)."""
import asyncio

import httpx
from httpx_sse import aconnect_sse

_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 15.0


async def stream_frames(client, session_id: str, headers_provider, *,
                        start_id: str = "0-0"):
    last_id = start_id or "0-0"
    backoff = _BACKOFF_BASE
    while True:
        headers = await headers_provider()
        headers["Last-Event-ID"] = last_id
        try:
            async with aconnect_sse(
                client, "GET", f"/v1/agent/sessions/{session_id}/stream",
                headers=headers,
            ) as event_source:
                backoff = _BACKOFF_BASE
                async for sse in event_source.aiter_sse():
                    if sse.id:
                        last_id = sse.id
                    yield sse.json()
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(min(backoff, _BACKOFF_MAX))
            backoff = min(backoff * 2, _BACKOFF_MAX)
