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
    """Raised for 401 (post-refresh) and every OTHER 4xx except 408/429 — the
    gateway's deterministic client-error verdicts (expired token that
    survived a forced refresh, ownership 403, not-found 404, and now
    400/402/405/413/422/... too): none of these are fixed by retrying, so
    they must surface immediately instead of hammering the endpoint forever.
    Distinct from httpx.HTTPError so it is NOT caught by the reconnect
    except-clause below — it propagates to the caller (repl.py's `except
    Exception` surfaces it). Everything else (5xx/408/429, transport drops)
    retries with Last-Event-ID resume — nothing is lost: the server keeps
    24h of stream."""


def _parse_frame(sse):
    """Parse one SSE event's data as JSON; skip (return None) a malformed/empty
    frame instead of letting JSONDecodeError abort the whole turn."""
    try:
        return sse.json()
    except (json.JSONDecodeError, ValueError):
        return None


_TRANSIENT_4XX = (408, 429)   # request-timeout / rate-limited — retry, not a verdict

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
                    # FIX3: every 4xx EXCEPT 408/429 is a deterministic verdict
                    # (401 keeps its one-refresh chance first) -- 403/404 and
                    # now 400/402/405/413/422/... too must surface immediately
                    # instead of looping as if they were transient.
                    if 400 <= status < 500 and status not in _TRANSIENT_4XX:
                        if status == 401 and force_refresh is not None and not auth_retried:
                            # Budget is consumed only by a COMPLETED refresh: if
                            # force_refresh itself fails transiently (gateway
                            # deploy mid-rotation), the retry clause below
                            # handles it and the one-per-window budget survives.
                            # A real logout raises NotLoggedInError here and
                            # propagates (verdict).
                            await force_refresh()
                            auth_retried = True
                            continue
                        raise StreamAuthError(f"stream {status}")
                    raise _TransientStatus(str(status))  # 5xx/408/429 → patient retry
                # FIX4: the connect SUCCEEDED (status check passed) -- transport
                # AND auth are confirmed back right here, not on the first frame.
                # Fire the online signal / reset state BEFORE entering the frame
                # loop so the toolbar drops "⟳ reconnecting" the instant the
                # connection is back, and a long frameless brain step is never
                # mistaken for a still-down stream (the >300s outage note must
                # measure the OUTAGE, not time-to-next-frame).
                if attempt and on_retry is not None:
                    try:
                        on_retry(0, 0.0)                 # back online
                    except Exception:
                        pass
                backoff, attempt, auth_retried = _BACKOFF_BASE, 0, False
                async for sse in event_source.aiter_sse():
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
