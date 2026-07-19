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
    """Raised ONLY for 401 (post-refresh) / 403 / 404 — the gateway's actual
    verdicts (expired token that survived a forced refresh, ownership 403,
    not-found). Distinct from httpx.HTTPError so it is NOT caught by the
    reconnect except-clause below — it propagates to the caller (repl.py's
    `except Exception` surfaces it) instead of hammering the endpoint
    forever. Everything else (5xx/408/429, transport drops) retries with
    Last-Event-ID resume — nothing is lost: the server keeps 24h of stream."""


def _parse_frame(sse):
    """Parse one SSE event's data as JSON; skip (return None) a malformed/empty
    frame instead of letting JSONDecodeError abort the whole turn."""
    try:
        return sse.json()
    except (json.JSONDecodeError, ValueError):
        return None


_VERDICT_STATUSES = (401, 403, 404)   # the gateway's REAL auth/ownership verdicts

try:                                   # taxonomy lands in imperal-mcp 0.5.2
    from imperal_mcp.auth import TransientAuthError as _TransientAuth
except ImportError:                    # older SDK: nothing to catch specially
    class _TransientAuth(Exception):
        ...


class _TransientStatus(Exception):
    """A retryable HTTP status on the stream connect (5xx/408/429): the edge
    proxy during a deploy, saturation. Distinct type so the retry clause
    catches it without catching StreamAuthError."""


async def stream_frames(client, session_id: str, headers_provider, *,
                        start_id: str = "0-0", force_refresh=None, on_retry=None):
    last_id = start_id or "0-0"
    backoff = _BACKOFF_BASE
    attempt = 0
    auth_retried = False   # ONE forced refresh per outage window
    while True:
        try:
            headers = await headers_provider()          # refresh inside try
            headers["Last-Event-ID"] = last_id
            async with aconnect_sse(
                client, "GET", f"/v1/agent/sessions/{session_id}/stream",
                headers=headers,
            ) as event_source:
                resp = getattr(event_source, "response", None)
                status = getattr(resp, "status_code", 0) if resp is not None else 0
                if status >= 400:
                    await resp.aread()
                    if status in _VERDICT_STATUSES:
                        if status == 401 and force_refresh is not None and not auth_retried:
                            # A locally-'valid' token the gateway revoked/rotated:
                            # ONE forced refresh, then reconnect. A second 401 is
                            # the real verdict.
                            auth_retried = True
                            await force_refresh()
                            continue
                        raise StreamAuthError(f"stream {status}")
                    raise _TransientStatus(str(status))  # 5xx/408/429 → patient retry
                async for sse in event_source.aiter_sse():
                    if attempt and on_retry is not None:
                        try:
                            on_retry(0, 0.0)             # back online
                        except Exception:
                            pass
                    backoff, attempt, auth_retried = _BACKOFF_BASE, 0, False
                    if sse.id:
                        last_id = sse.id
                    frame = _parse_frame(sse)
                    if frame is not None:
                        yield frame
        except (httpx.HTTPError, OSError, _TransientStatus, _TransientAuth):
            attempt += 1
            delay = min(backoff, _BACKOFF_MAX)
            if on_retry is not None:
                try:
                    on_retry(attempt, delay)
                except Exception:
                    pass
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, _BACKOFF_MAX)
