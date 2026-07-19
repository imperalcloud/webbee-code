"""ONE keep-alive httpx.AsyncClient for the client's small gateway calls.
Before W1 the idle-steer poller opened a NEW AsyncClient — a fresh TCP+TLS
handshake — every 4s tick, forever (thread.py per-call clients). The repl owns
one client for the loop's lifetime and passes it down; every callee keeps a
per-call fallback so tests and old call sites work unchanged."""
from __future__ import annotations


def make_client(cfg):
    import httpx
    return httpx.AsyncClient(base_url=cfg.api_url, timeout=10)
