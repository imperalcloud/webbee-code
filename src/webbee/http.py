"""ONE keep-alive httpx.AsyncClient for the client's small gateway calls.
Before W1 the idle-steer poller opened a NEW AsyncClient — a fresh TCP+TLS
handshake — every 4s tick, forever (thread.py per-call clients). The repl owns
one client for the loop's lifetime and passes it down; every callee keeps a
per-call fallback so tests and old call sites work unchanged.

Sleep and network changes: the sockets carry TCP keepalive, so a connection
that died while the laptop slept is found dead within about a minute instead of
hanging a stream forever; idle pooled connections expire after 30s, so the next
call after a wake opens a fresh one. ``WakeDetector`` tells a reconnect loop the
machine slept, so it reconnects at once instead of waiting out its backoff."""
from __future__ import annotations

import socket
import time

# keepalive probes: first after 30s idle, then every 10s, dead after 3 misses
_KEEPIDLE_S, _KEEPINTVL_S, _KEEPCNT = 30, 10, 3
_SLEEP_GAP_S = 8.0


class WakeDetector:
    """Did the machine sleep since the last ``check()``? The wall clock keeps running while a laptop
    sleeps and the monotonic clock does not (Linux, macOS), so their drift is the time asleep. A long
    idle wait is never a sleep: both clocks move together. One instance per loop -- never shared."""

    def __init__(self, gap_s: float = _SLEEP_GAP_S, *, wall=time.time, mono=time.monotonic):
        self._gap, self._wall, self._mono = float(gap_s), wall, mono
        self._w, self._m = wall(), mono()

    def check(self) -> bool:
        w, m = self._wall(), self._mono()
        slept = (w - self._w) - (m - self._m) > self._gap
        self._w, self._m = w, m
        return slept


def socket_options() -> list:
    """TCP keepalive options this OS supports (Linux: TCP_KEEPIDLE; macOS: TCP_KEEPALIVE)."""
    opts = []
    if hasattr(socket, "SO_KEEPALIVE"):
        opts.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
    for idle in ("TCP_KEEPIDLE", "TCP_KEEPALIVE"):
        if hasattr(socket, idle):
            opts.append((socket.IPPROTO_TCP, getattr(socket, idle), _KEEPIDLE_S))
            break
    if hasattr(socket, "TCP_KEEPINTVL"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KEEPINTVL_S))
    if hasattr(socket, "TCP_KEEPCNT"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPCNT))
    return opts


def build_transport(retries: int = 1):
    """An AsyncHTTPTransport with TCP keepalive and a short idle expiry (``retries``: connect retries)."""
    import httpx
    return httpx.AsyncHTTPTransport(
        retries=retries, socket_options=socket_options(),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0))


def make_client(cfg, timeout: float = 10.0):
    import httpx
    return httpx.AsyncClient(base_url=cfg.api_url, timeout=timeout, transport=build_transport())
