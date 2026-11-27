"""Background work in full on the person's machine (the owner, 2026-11-27: "to throw it into the background myself,
to come back to it, to ask about it, to see its result ... even hot keys").

Everything runs for real in a throwaway HOME: a command the terminal waits for is a job on the machine from its
first moment, so sending it to the background touches nothing of the process."""
import asyncio
import os
import threading
import time

import pytest

from imperal_sdk import session_jobs as sj
from webbee import jobs, jobwatch
from webbee.commands import CommandContext, dispatch

pytestmark = pytest.mark.skipif(os.name == "nt", reason="jobs are POSIX")
SID = "marathon-imp_u_x-r8ff65fbc8014"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "home_dir", lambda: str(tmp_path))
    return str(tmp_path)


def _held(command, home):
    return jobs.start_held(command, cwd=home, env={"PATH": os.environ["PATH"], "HOME": home})


def _until(fn, seconds=10.0):
    t = time.time()
    while time.time() - t < seconds:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    return fn()


# ---- a command the terminal waits for -------------------------------------------------------------------------------

def test_a_waited_command_gives_its_output_live_and_its_code_and_leaves_nothing(home):
    pieces = []
    held = _held("echo one; sleep 0.3; echo two >&2; exit 5", home)
    raw, how = jobs.follow(held, 20, pieces.append)
    assert how == "done" and raw == b"one\ntwo\n" and held.proc.returncode == 5
    assert b"".join(pieces) == raw and len(pieces) >= 2                       # as it was printed, not at the end
    jobs.drop(held)
    assert not os.path.exists(held.folder) and jobs.mine(SID, home) == []


def test_a_bound_that_passes_is_said_and_the_command_is_not_ended_by_the_wait(home):
    held = _held("echo started; sleep 30", home)
    raw, how = jobs.follow(held, 0.5)
    assert how == "timeout" and raw == b"started\n" and held.proc.poll() is None   # the caller stops it, not the wait
    os.killpg(held.proc.pid, 15)
    held.proc.wait(10)


def test_sent_to_the_background_the_command_runs_on_as_a_job_and_ends_by_its_own_code(home):
    held = _held("echo first; sleep 1; echo last; exit 3", home)
    got = {}
    waiter = threading.Thread(target=lambda: got.update(zip(("raw", "how"), jobs.follow(held, 60))))
    waiter.start()
    assert _until(lambda: b"first" in open(os.path.join(held.folder, "log"), "rb").read())
    path = jobs.link(held, SID, 4, "the   suite\nrun")
    jobs.release(held, 4)
    waiter.join(5)
    assert got == {"raw": b"first\n", "how": "released"} and held.proc.poll() is None    # the process was not touched
    jobs.drop(held)                                                            # a released command keeps its folder
    mine = jobs.mine(SID, home)
    assert [(j["n"], j["id"], j["title"], j["command"], j["reported"]) for j in mine] == \
        [(4, "j4", "the suite run", "echo first; sleep 1; echo last; exit 3", "")]
    assert mine[0]["path"] == path and jobs.state_of(SID, 4, home)["state"] == sj.RUNNING
    end = _until(lambda: (lambda g: g if g["state"] in sj.ENDED else None)(jobs.state_of(SID, 4, home)))
    assert (end["state"], end["exit_code"], end["tail"]) == (sj.FAILED, 3, "first\nlast\n")
    assert jobs.log_tail(path) == "first\nlast\n" and "failed (exit 3)" in jobs.line_of(mine[0], end)
    with pytest.raises(FileExistsError):                                       # a number is never given twice here
        jobs.link(_held("true", home), SID, 4)


def test_a_job_is_ended_from_the_terminal_and_only_a_running_one(home):
    held = _held("sleep 60", home)
    jobs.link(held, SID, 1, "nap")
    jobs.release(held, 1)
    assert jobs.find(SID, "j1", home)["n"] == 1 and jobs.find(SID, "2", home) is None and jobs.find(SID, "x", home) is None
    sent, said = jobs.cancel(SID, 1, home)
    assert sent and "cancel sent" in said
    assert _until(lambda: jobs.state_of(SID, 1, home)["state"] in sj.ENDED)
    assert jobs.state_of(SID, 1, home)["state"] == sj.CANCELLED
    again, said = jobs.cancel(SID, 1, home)
    assert not again and "cancel failed" in said
    assert jobs.cancel(SID, 9, home)[0] is False


def test_the_jobs_of_another_session_or_slot_are_not_this_sessions(home):
    for sid, n in ((SID, 1), (SID + "-s2", 1), ("marathon-imp_u_y-r8ff65fbc8014", 2)):
        h = _held("true", home)
        jobs.link(h, sid, n)
        jobs.release(h, n)
    os.makedirs(os.path.join(jobs.root(home), sj.session_part(SID) + "-7"))    # a start that never ran: no pid
    assert [j["n"] for j in jobs.mine(SID, home)] == [1]
    assert [j["n"] for j in jobs.mine(SID + "-s2", home)] == [1]


def test_prune_removes_only_what_nobody_reads_again(home):
    old = _held("true", home)
    jobs.link(old, SID, 1)
    jobs.release(old, 1)
    fresh = _held("true", home)
    jobs.link(fresh, SID, 2)
    jobs.release(fresh, 2)
    untold = _held("true", home)
    jobs.link(untold, SID, 3)
    jobs.release(untold, 3)
    live = _held("sleep 30", home)
    dead = _held("true", home)
    for h in (old, fresh, untold, dead):
        h.proc.wait(10)
    for h in (old, fresh):
        jobs.mark_reported(h.folder, "done")
    now = time.time()
    long_ago = now - 30 * 86400
    for h in (old, untold):
        os.utime(os.path.join(h.folder, "pid"), (long_ago, long_ago))
    os.utime(dead.folder, (now - 7200, now - 7200))
    os.utime(live.folder, (now - 7200, now - 7200))
    outside = os.path.join(home, "precious")                  # a link planted among the runs leads out of them
    os.makedirs(outside)
    with open(os.path.join(outside, "pid"), "w") as f:
        f.write("999999")
    os.utime(outside, (now - 7200, now - 7200))
    os.symlink(outside, os.path.join(jobs.root(home), sj.RUNS, "planted"))
    assert jobs.prune(home, now) == 2                         # the old told job, and the run of a dead terminal
    assert os.path.isfile(os.path.join(outside, "pid"))       # what a link leads to is never removed
    assert [j["n"] for j in jobs.mine(SID, home)] == [2, 3]   # a fresh one stays; an old UNTOLD one stays too
    assert not os.path.exists(old.folder) and not os.path.exists(dead.folder) and os.path.isdir(live.folder)
    os.killpg(live.proc.pid, 15)


# ---- the shell tool -------------------------------------------------------------------------------------------------

def test_the_shell_tool_answers_as_before_and_a_released_command_is_never_ok(home, tmp_path):
    from webbee.tools import LocalToolExecutor
    ex = LocalToolExecutor(str(tmp_path))
    seen = []
    ex.on_output = seen.append
    done = ex.run("bash", {"command": "echo a; echo b; [[ 1 == 1 ]] && exit 7"})
    assert (done["ok"], done["exit_code"]) == (False, 7) and done["content"] == "a\nb\n\n[exit 7]" and "b" in seen
    assert ex.run("bash", {"command": "printf ok"})["content"] == "ok" and ex.held_running() == []
    assert os.listdir(os.path.join(jobs.root(home), sj.RUNS)) == []            # nothing left of a waited command
    slow = ex.run("bash", {"command": "echo x; sleep 30", "timeout": 1})
    assert slow["timed_out"] and slow["ok"] is False and slow["content"].startswith("x\n")

    out = {}
    worker = threading.Thread(target=lambda: out.update(ex.run("bash", {"command": "echo up; sleep 30"})))
    worker.start()
    held = _until(lambda: ex.held_running())[0]
    assert _until(lambda: b"up" in open(os.path.join(held.folder, "log"), "rb").read())
    jobs.link(held, SID, 2, "nap")
    jobs.release(held, 2)
    worker.join(5)
    assert out["ok"] is False and out["backgrounded"] is True and out["job"] == "j2" and "exit_code" not in out
    assert out["content"].startswith("[SENT TO THE BACKGROUND BY THE PERSON -- NOT FINISHED") and "up" in out["content"]
    assert ex.held_running() == [] and jobs.state_of(SID, 2, home)["state"] == sj.RUNNING
    assert jobs.cancel(SID, 2, home)[0]


def test_a_stop_ends_a_waited_command_and_every_process_it_started(home, tmp_path):
    from webbee.tools import LocalToolExecutor
    ex = LocalToolExecutor(str(tmp_path))
    out = {}
    worker = threading.Thread(target=lambda: out.update(ex.run("bash", {"command": "sleep 41 & sleep 42"})))
    worker.start()
    held = _until(lambda: ex.held_running())[0]
    assert ex.cancel_running() == 1
    worker.join(10)
    assert not worker.is_alive() and out["ok"] is False and not jobs._alive(held.proc.pid)


# ---- the session's side ---------------------------------------------------------------------------------------------

class _Agent:
    def __init__(self, executor, answers):
        from webbee.session import AgentSession
        self._executor, self.session_id, self.calls, self._answers = executor, SID, [], list(answers)
        self.background_running = AgentSession.background_running.__get__(self)

    async def _job_door(self, sid, door, body):
        self.calls.append((sid, door, body))
        return self._answers.pop(0)


def test_the_person_sends_the_waited_command_to_the_background_only_with_the_kernels_number(home, tmp_path):
    from webbee.tools import LocalToolExecutor
    ex = LocalToolExecutor(str(tmp_path))
    out = {}
    worker = threading.Thread(target=lambda: out.update(ex.run("bash", {"command": "echo go; sleep 30"})))
    worker.start()
    held = _until(lambda: ex.held_running())[0]
    agent = _Agent(ex, [None, {"ok": False, "error": "this session already holds 9999 jobs"},
                        {"ok": True, "n": 3, "dir": "$HOME/.imperal-jobs/elsewhere-3"},
                        {"ok": True, "n": 3, "dir": sj.job_dir(SID, 3)}])
    refused = [asyncio.run(agent.background_running())[0] for _ in range(3)]
    assert "could not be reached" in refused[0] and "9999 jobs" in refused[1] and "no job number" in refused[2]
    assert all("stays in front" in r for r in refused) and worker.is_alive() and jobs.mine(SID, home) == []
    said = asyncio.run(agent.background_running())
    worker.join(5)
    assert said == ["j3 in the background: `echo go; sleep 30` -- /jobs lists, /job 3 shows its output, "
                    "/job 3 cancel ends it"]
    sid, door, body = agent.calls[-1]
    assert (sid, door) == (SID, "adopt") and body["command"] == "echo go; sleep 30" and body["started_ts"] == held.started
    assert out["job"] == "j3" and jobs.state_of(SID, 3, home)["state"] == sj.RUNNING
    assert asyncio.run(agent.background_running()) == []                        # nothing is waited for now
    jobs.cancel(SID, 3, home)


# ---- the watch ------------------------------------------------------------------------------------------------------

def test_a_state_is_told_when_new_when_changed_and_once_a_heartbeat():
    assert jobwatch.due(None, "running", 0.0)
    assert not jobwatch.due(("running", 0.0), "running", 59.0) and jobwatch.due(("running", 0.0), "running", 60.0)
    assert jobwatch.due(("running", 0.0), "done", 1.0)


def _watch(home, answers, ticks, clock, counts=None):
    posted, shown = [], []

    async def post(sid, n, out):
        posted.append((sid, n, sj.read_poll(out)["state"]))
        return answers.pop(0) if answers else None

    async def sid():
        return SID

    async def run():
        left = [ticks]

        async def sleep(_):
            left[0] -= 1
            if left[0] < 0:
                raise asyncio.CancelledError
            clock[0] += 5.0
        with pytest.raises(asyncio.CancelledError):
            await jobwatch.watch(session_id=sid, post=post, on_line=shown.append, home=home, _sleep=sleep,
                                 on_count=counts.append if counts is not None else None,
                                 _monotonic=lambda: clock[0])
    asyncio.run(run())
    return posted, shown


def test_the_watch_tells_the_kernel_the_raw_poll_and_shows_the_line_the_kernel_answered(home):
    held = _held("sleep 0.2; exit 2", home)
    jobs.link(held, SID, 1, "suite")
    jobs.release(held, 1)
    held.proc.wait(10)
    clock = [0.0]
    # the first report does not get through: the same state is told again a little later, then taken
    posted, shown = _watch(home, [None, {"ok": True, "state": "failed", "ended": True, "already": False,
                                         "line": 'j1 "suite": failed (exit 2)'}], 4, clock)
    assert posted == [(SID, 1, sj.FAILED), (SID, 1, sj.FAILED)] and shown == ['j1 "suite": failed (exit 2)']
    assert jobs.mine(SID, home)[0]["reported"] == "failed"
    assert _watch(home, [], 3, clock) == ([], [])                               # told: nothing more is sent, ever


def test_a_running_job_is_told_once_and_then_by_heartbeat_and_an_end_the_kernel_already_holds_is_not_shown(home):
    held = _held("sleep 30", home)
    jobs.link(held, SID, 2, "nap")
    jobs.release(held, 2)
    clock = [0.0]
    running = {"ok": True, "state": "running", "ended": False, "already": False, "line": "j2 running"}
    counts = []
    posted, shown = _watch(home, [dict(running), dict(running)], 13, clock, counts)    # 65 s of ticks
    assert posted == [(SID, 2, sj.RUNNING), (SID, 2, sj.RUNNING)] and shown == []
    assert counts == [1] * 13                                                   # the toolbar's number, every look
    jobs.cancel(SID, 2, home)
    assert _until(lambda: jobs.state_of(SID, 2, home)["state"] in sj.ENDED)
    posted, shown = _watch(home, [{"ok": True, "state": "cancelled", "ended": True, "already": True,
                                   "line": "j2 cancelled"}], 2, clock, counts)
    assert counts[-2:] == [0, 0]
    assert posted == [(SID, 2, sj.CANCELLED)] and shown == [] and jobs.mine(SID, home)[0]["reported"] == "cancelled"


def test_a_folder_the_kernel_does_not_know_stops_being_told_only_once_it_has_ended(home):
    held = _held("true", home)
    jobs.link(held, SID, 5, "stray")
    jobs.release(held, 5)
    held.proc.wait(10)
    no = {"ok": False, "error": "this session has no job j5 on the person's own machine"}
    clock = [0.0]
    posted, _ = _watch(home, [dict(no)] * 3, 40, clock)                         # 200 s: three heartbeats of "no"
    assert len(posted) == 3 and jobs.mine(SID, home)[0]["reported"] == "unknown"


# ---- the words ------------------------------------------------------------------------------------------------------

def test_the_commands_of_background_work():
    ctx = CommandContext(mode="default", workspace="/w", version="0", surface="terminal", logged_in=True,
                         session_tokens=0, session_credits=0, git_branch="")
    assert dispatch("/jobs", ctx).action == "jobs" and dispatch("/job", ctx).action == "jobs"
    one = dispatch("/job J3", ctx)
    assert (one.action, one.arg) == ("job", "j3")
    end = dispatch("/job 3 CANCEL now", ctx)
    assert (end.action, end.arg) == ("job", "3 cancel")
    assert dispatch("/bg", ctx).action == "background"
    text = dispatch("/help", ctx).message
    assert "/jobs" in text and "/job N [cancel]" in text and "Ctrl+Z" in text and "Ctrl+O" in text


def test_a_home_that_cannot_hold_jobs_still_runs_the_command(home, tmp_path, monkeypatch):
    """A read-only home or a full disk takes "to the background" away, never the command itself."""
    from webbee.tools import LocalToolExecutor

    def refuse(*a, **k):
        raise PermissionError("read-only home")
    monkeypatch.setattr(jobs, "start_held", refuse)
    ex = LocalToolExecutor(str(tmp_path))
    got = ex.run("bash", {"command": "echo still; [[ 1 == 1 ]] && exit 4"})
    assert (got["ok"], got["exit_code"], got["content"]) == (False, 4, "still\n\n[exit 4]") and ex.held_running() == []
