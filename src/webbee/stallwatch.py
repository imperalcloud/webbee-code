"""Where the terminal stands when it stops answering -- written down by the terminal itself.

Live 2026-11-26/27, five times in one day on one machine: the kernel sent a command, the terminal never answered and
never said why; the connection stayed open. From the outside nothing tells where the client stood -- its event loop
blocked, a tool that never returned, a result that could not be posted. So the client says it: a thread outside the
loop watches two facts and, when one of them holds too long, writes every thread's stack to a file once.

* the LOOP does not turn: a callback handed to it is not run within ``LOOP_S``;
* a STAGE lasts too long: the turn said what it is doing (``mark("tool bash")``, ``mark("post result")``) and has
  not said anything else for ``STAGE_S``.

The file is ``~/.cache/webbee/stalls/stall-<time>.txt``: the stage, how long, the version, the stacks. Nothing is
sent anywhere; the person (or `webbee doctor`) reads it. ``SIGUSR1`` writes the same file on demand where the
platform has it. Python 3.7 syntax; never raises into the client."""
import faulthandler
import os
import sys
import threading
import time

LOOP_S = 15.0        # a blocked loop is a frozen terminal: said after this long
STAGE_S = 300.0      # a tool of five minutes is long but real; the stack then shows what it waits on
TICK_S = 1.0
KEPT = 20            # stall files kept (newest)

_state = {"stage": "", "since": 0.0, "beat": 0.0, "started": False, "dir": ""}
_lock = threading.Lock()


def stalls_dir() -> str:
    return _state["dir"] or os.path.join(os.path.expanduser("~"), ".cache", "webbee", "stalls")


def mark(stage: str = "") -> None:
    """What the turn is doing now ("" -- nothing that can stall: it is reading the stream)."""
    with _lock:
        _state["stage"], _state["since"] = str(stage or ""), time.monotonic()


def _version() -> str:
    try:
        from webbee import __version__
        return str(__version__)
    except Exception:
        return "?"


def dump(why: str, directory: str = "") -> str:
    """Write every thread's stack to a new file; its path, or "" when it could not be written."""
    try:
        d = directory or stalls_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "stall-%s-%d.txt" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        with open(path, "w") as f:
            f.write("webbee %s  python %s  pid %d\n%s\nthreads:\n" % (
                _version(), sys.version.split()[0], os.getpid(), why))
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
        old = sorted(n for n in os.listdir(d) if n.startswith("stall-"))[:-KEPT]
        for n in old:
            try:
                os.unlink(os.path.join(d, n))
            except OSError:
                pass
        return path
    except Exception:
        return ""


def verdict(now: float, beat: float, stage: str, since: float, loop_s: float = LOOP_S, stage_s: float = STAGE_S) -> str:
    """PURE. Why the client is stalled at ``now``, or "" while it is not."""
    if now - beat >= loop_s:
        return "the event loop has not turned for %.0f s (stage: %s)" % (now - beat, stage or "reading the stream")
    if stage and now - since >= stage_s:
        return "the stage '%s' has lasted %.0f s" % (stage, now - since)
    return ""


def ensure(loop, on_stall=None, loop_s: float = LOOP_S, stage_s: float = STAGE_S, tick: float = TICK_S,
           directory: str = "") -> bool:
    """Start the watch for this process once. ``on_stall(path, why)`` is called from the watch thread after a file
    was written (a surface may show a note). False when it was already running."""
    with _lock:
        if _state["started"]:
            return False
        _state["started"], _state["beat"], _state["dir"] = True, time.monotonic(), directory

    def pong() -> None:
        _state["beat"] = time.monotonic()

    def watch() -> None:
        said = ""
        while True:
            time.sleep(tick)
            try:
                if loop.is_closed():
                    return
                loop.call_soon_threadsafe(pong)
            except Exception:
                return
            with _lock:
                stage, since = _state["stage"], _state["since"]
            why = verdict(time.monotonic(), _state["beat"], stage, since, loop_s, stage_s)
            key = why.split(" for ")[0].split(" has lasted ")[0]       # one file per stall, not one a second
            if why and key != said:
                said = key
                path = dump(why, directory)
                if path and on_stall is not None:
                    try:
                        on_stall(path, why)
                    except Exception:
                        pass
            elif not why:
                said = ""

    threading.Thread(target=watch, name="webbee-stall-watch", daemon=True).start()
    try:                                   # on demand, where the platform has the signal
        import signal
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, lambda *_: dump("asked for by SIGUSR1 (stage: %s)" % (_state["stage"] or "-")))
    except Exception:
        pass
    return True
