"""The event loop's own exception handler: a peer closing a socket is no error, anything else goes to the log.

Live 2026-11-14 (users on Windows, Python 3.11): the full-screen dock printed "Unhandled exception in event loop ...
proactor_events.py ... _call_connection_lost ... self._sock.shutdown(socket.SHUT_RDWR) ... [WinError 10054] An existing
connection was forcibly closed by the remote host" and stopped on "Press ENTER to continue..." -- prompt_toolkit's own
loop exception handler (Application.run_async(set_exception_handler=True), its default) prints any unhandled loop
exception over the screen and waits for a key. On Windows the ProactorEventLoop's connection-lost callback calls
shutdown() on a socket the server (or a balancer) already reset; the exception has no task to land in, so it reaches
the loop's handler. Nothing is lost there: the connection is gone either way and the client opens a new one.

So: the dock runs with set_exception_handler=False and this handler on the loop -- a reset / aborted / broken
connection is dropped silently, every other loop exception goes to the "webbee" logger (cli._log_to_file: webbee.log)
and never painted over the screen.
"""
from __future__ import annotations

import asyncio
import logging

_PEER_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
_log = logging.getLogger("webbee.loop")


def handle_loop_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """``loop.set_exception_handler`` callback (see the module doc)."""
    exc = context.get("exception")
    if isinstance(exc, _PEER_GONE):
        return
    msg = str(context.get("message") or "unhandled exception in event loop")
    if exc is not None:
        _log.warning("%s: %s: %s", msg, type(exc).__name__, exc, exc_info=exc)
    else:
        _log.warning("%s (%s)", msg, {k: repr(v)[:200] for k, v in context.items() if k != "message"})


def install(loop: "asyncio.AbstractEventLoop | None" = None) -> None:
    """Put the handler on ``loop`` (default: the running loop)."""
    (loop or asyncio.get_running_loop()).set_exception_handler(handle_loop_exception)
