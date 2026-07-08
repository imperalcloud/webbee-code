"""Reconnecting SSE frame iterator (U0). The gateway serves the durable
down-stream with SSE ids; on any transport drop we reconnect and resume from
the last seen id — no frame is ever lost (server keeps 24h of stream). The
caller breaks the async-for on its terminal frame (final)."""
import asyncio
import json

import httpx
from httpx_sse import aconnect_sse

_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 15.0


class StreamAuthError(Exception):
    """Raised for a non-retryable 4xx from the stream endpoint (expired
    token, ownership 403, quota). Distinct from httpx.HTTPError so it is NOT
    caught by the reconnect except-clause below — it propagates to the
    caller (repl.py's `except Exception` surfaces it) instead of hammering
    the endpoint forever."""


def _parse_frame(sse):
    """Parse one SSE event's data as JSON; skip (return None) a malformed/empty
    frame instead of letting JSONDecodeError abort the whole turn."""
    try:
        return sse.json()
    except (json.JSONDecodeError, ValueError):
        return None


async def stream_frames(client, session_id: str, headers_provider, *,
                        start_id: str = "0-0"):
    last_id = start_id or "0-0"
    backoff = _BACKOFF_BASE
    while True:
        try:
            headers = await headers_provider()          # refresh inside try
            headers["Last-Event-ID"] = last_id
            async with aconnect_sse(
                client, "GET", f"/v1/agent/sessions/{session_id}/stream",
                headers=headers,
            ) as event_source:
                resp = getattr(event_source, "response", None)
                if resp is not None and resp.status_code >= 400 and resp.status_code not in (408, 429):
                    # auth/ownership/quota — surface immediately, do NOT retry
                    await resp.aread()
                    raise StreamAuthError(f"stream {resp.status_code}")
                async for sse in event_source.aiter_sse():
                    backoff = _BACKOFF_BASE            # reset only after a real event
                    if sse.id:
                        last_id = sse.id
                    frame = _parse_frame(sse)
                    if frame is not None:
                        yield frame
        except (httpx.HTTPError, OSError):
            await asyncio.sleep(min(backoff, _BACKOFF_MAX))
            backoff = min(backoff * 2, _BACKOFF_MAX)
