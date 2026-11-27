"""The terminal's watch over the jobs of its session on this machine (one-loop plan, rule 8, design
``p8-local-jobs-design-v1.md``).

The kernel reaches the person's machine only through their terminal, so the terminal reads its jobs and TELLS the
kernel: it runs each unfinished job's poll line (the kernel's own read-only line, ``imperal_sdk.session_jobs``) and
posts the line's raw output to the gateway's door. It says nothing of the state itself -- the kernel reads the output
with its own reader, writes the record and, at a job's end, tells the session in its own words. The terminal only
shows the line the kernel answered with.

A report goes when the state changed and, while nothing changes, once per ``HEARTBEAT_S`` -- the kernel reads
three missed heartbeats as silence (``core.session_jobs.QUIET_S``). Jobs are found on disk at every tick, so one that
ended while the terminal was closed is reported at the terminal's next start with no list kept anywhere else."""
from __future__ import annotations

import asyncio
import time

from imperal_sdk import session_jobs as sj

from webbee import jobs

TICK_S = 5.0            # one look at the session's jobs; the look is local and reads four small files a job
HEARTBEAT_S = 60.0      # a running job is told of this often while nothing changes
RETRY_S = 15.0          # a report that did not get through is sent again after this, not at every look
MISSES_KNOWN = 3        # the kernel said this often that the session has no such job: it is not the session's
PRUNE_EVERY_S = 3600.0


def due(prev, state: str, now: float, heartbeat: float = HEARTBEAT_S) -> bool:
    """PURE. Whether a job's state is told now: never told, changed since, or a heartbeat has passed."""
    return prev is None or prev[0] != state or now - prev[1] >= heartbeat


async def watch(*, session_id, post, on_line=None, on_count=None, tick: float = TICK_S,
                heartbeat: float = HEARTBEAT_S,
                home: str = "", _monotonic=None, _sleep=None) -> None:
    """Run until cancelled. Seams, all given by the terminal's wiring:
      * session_id() -- async; the session this terminal's turns use ("" while it is not known yet);
      * post(sid, n, stdout) -- async; sends one report and returns the kernel's answer
        (``{ok, state, ended, already, line}``) or None when it did not get through (told again at the next tick);
      * on_line(text) -- sync; shows the person a job's line when the kernel took its end;
      * on_count(n) -- sync; how many of the session's jobs run on this machine now (the toolbar's number).
    Fail-soft: any error skips the tick; the jobs are on disk and the next tick reads them again."""
    if not jobs.supported():
        return
    now = _monotonic or time.monotonic
    sleep = _sleep or asyncio.sleep
    told: dict = {}        # (sid, n) -> (state, when)
    misses: dict = {}
    retry: dict = {}       # (sid, n) -> not before
    pruned = None
    while True:
        await sleep(tick)
        try:
            if pruned is None or now() - pruned >= PRUNE_EVERY_S:
                pruned = now()
                await asyncio.to_thread(jobs.prune, home)
            sid = str(await session_id() or "")
            if not sid:
                continue
            running = 0
            for job in await asyncio.to_thread(jobs.mine, sid, home):
                if job["reported"]:
                    continue
                key = (sid, job["n"])
                out = await asyncio.to_thread(jobs.poll, sid, job["n"], home)
                state = sj.read_poll(out)["state"]
                running += state == sj.RUNNING
                if not state or retry.get(key, 0.0) > now() or not due(told.get(key), state, now(), heartbeat):
                    continue
                answer = await post(sid, job["n"], out)
                if not isinstance(answer, dict):
                    retry[key] = now() + RETRY_S          # not through (the network, a gateway without the door):
                    continue                              # the same state is told again, a little later
                told[key] = (state, now())
                if answer.get("ok"):
                    misses.pop(key, None)
                    if answer.get("ended"):
                        jobs.mark_reported(job["path"], str(answer.get("state") or state))
                        if on_line is not None and not answer.get("already"):
                            on_line(str(answer.get("line") or jobs.line_of(job, {"state": state})))
                    continue
                misses[key] = misses.get(key, 0) + 1
                if misses[key] >= MISSES_KNOWN and state in sj.ENDED:
                    # the kernel holds no such job of this session (a folder of another time): nothing more to tell
                    jobs.mark_reported(job["path"], "unknown")
            if on_count is not None:
                on_count(running)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
