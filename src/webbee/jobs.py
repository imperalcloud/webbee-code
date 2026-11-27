"""The jobs of a session ON THIS MACHINE, as the terminal sees them (one-loop plan 2026-11-26, rule 8; the owner,
2026-11-27: "to throw it into the background myself, to come back to it, to ask about it, to see its result").

A job is a folder under ``~/.imperal-jobs`` -- its log, its exit code, its process -- in the form the SDK owns
(``imperal_sdk.session_jobs``): the kernel writes the lines that start, read and end one, this module runs them here
and never writes a shell line of its own.

One form for every command of the terminal: a command the terminal WAITS for runs in that same form (``start_held``
-- its output goes to a file, never down a pipe to this process). So "to the background" changes nothing of the
process: the terminal stops waiting, the folder gets the job's name (``link``) and the command outlives the
terminal. Before, a command's output was a pipe to the terminal and the command lived exactly as long as the call
that waited for it.

Everything here is this machine's files and processes; no network. POSIX only: on Windows the terminal keeps its
old way of running a command and has no jobs."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from imperal_sdk import session_jobs as sj

FOLLOW_TICK_S = 0.1          # how often a waiting terminal reads its command's log
POLL_TIMEOUT_S = 10.0        # a poll line reads four small files: longer than this is a hung machine, not a job
KEEP_DAYS = 14               # an ended job's folder stays this long: its log is what "see its result" reads
REPORTED = "reported"        # in a job's folder: the kernel holds its end (the state it took), nothing more to tell
_SEQ = [0]
_SEQ_LOCK = threading.Lock()


def supported() -> bool:
    return os.name != "nt"


def home_dir() -> str:
    return os.path.expanduser("~")


def root(home: str = "") -> str:
    return os.path.join(home or home_dir(), ".imperal-jobs")


def _env(home: str = "") -> dict:
    return {"HOME": home or home_dir(), "PATH": os.environ.get("PATH") or "/usr/bin:/bin"}


def _read(path: str, limit: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


# ---- a command the terminal waits for ---------------------------------------------------------------------------

class Held:
    """One command the terminal started and waits for. ``released`` is set when the person sends it to the
    background (``job`` then holds its number); ``folder`` is where it writes."""
    __slots__ = ("command", "folder", "proc", "started", "released", "job", "why")

    def __init__(self, command: str, folder: str, proc, started: float) -> None:
        self.command, self.folder, self.proc, self.started = command, folder, proc, started
        self.released = threading.Event()
        self.job = 0
        self.why = ""


def start_held(command: str, *, cwd: str, env: dict, shell: str = "", home: str = "") -> Held:
    """Start ``command`` in the job's form and return at once: a new session (its own process group), output to the
    run folder's ``log``, its exit code written by the command's own wrapper (``sj.held_command``). The folder is the
    terminal's own (``.runs/<name>``) until the command is adopted as a job."""
    with _SEQ_LOCK:
        _SEQ[0] += 1
        seq = _SEQ[0]
    folder = os.path.join(root(home), sj.RUNS, f"{int(time.time())}.{os.getpid()}.{seq}")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    with open(os.path.join(folder, "cmd"), "w", encoding="utf-8") as f:
        f.write(command)
    sh = shell or shutil.which("bash") or "sh"
    with open(os.path.join(folder, "log"), "wb") as log:
        proc = subprocess.Popen([sh, "-c", sj.held_command(command, folder, sh)], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    with open(os.path.join(folder, "pid"), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    return Held(command, folder, proc, time.time())


def follow(held: Held, timeout: float, on_piece=None, tick: float = FOLLOW_TICK_S) -> tuple:
    """Wait for a held command, reading its log AS IT GROWS: ``(all it wrote until now, how)`` where ``how`` is
    ``"done"`` (it ended), ``"released"`` (the person sent it to the background -- it runs on) or ``"timeout"``
    (the caller's bound passed; the caller stops it). ``on_piece(bytes)`` gets each new piece the moment it is read;
    it never breaks the wait."""
    chunks: list = []
    began = time.monotonic()
    how = ""
    with open(os.path.join(held.folder, "log"), "rb") as log:
        def take() -> None:
            while True:
                piece = log.read(65536)
                if not piece:
                    return
                chunks.append(piece)
                if on_piece is not None:
                    try:
                        on_piece(piece)
                    except Exception:
                        pass
        while not how:
            take()
            if held.released.is_set():
                how = "released"
            elif held.proc.poll() is not None:
                how = "done"
            elif time.monotonic() - began >= timeout:
                how = "timeout"
            else:
                time.sleep(tick)
        take()
    return b"".join(chunks), how


def release(held: Held, n: int) -> None:
    """The wait for ``held`` ends: it is job ``n`` now. From here nobody waits for the process, so a thread takes its
    end when it comes -- without that the ended process stays a zombie of this terminal, and a zombie's group still
    answers a signal: the job would read as running until the terminal closed (found by this module's own test)."""
    held.job = int(n)
    threading.Thread(target=held.proc.wait, name="webbee-job-end", daemon=True).start()
    held.released.set()


def drop(held: Held) -> None:
    """A command that ended while the terminal waited leaves nothing behind: its output went back with the call."""
    if not held.released.is_set():
        shutil.rmtree(held.folder, ignore_errors=True)


def link(held: Held, session_id: str, n: int, title: str = "", home: str = "") -> str:
    """The held command becomes job ``n`` of ``session_id`` on this machine: the job's name leads to its run folder,
    so the SDK's lines read it as any job. Raises ``FileExistsError`` when that job's folder is already there (a
    number is never given twice on one machine) -- the caller then leaves the command in the foreground."""
    dest = sj.local_dir(sj.job_dir(session_id, n), home or home_dir())
    os.symlink(held.folder, dest)                       # fails, atomically, if the name is taken
    with open(os.path.join(held.folder, "title"), "w", encoding="utf-8") as f:
        f.write(" ".join(str(title or "").split())[:sj.TITLE_CHARS])
    return dest


# ---- the jobs of a session, read from this machine ----------------------------------------------------------------

def mine(session_id: str, home: str = "") -> list:
    """The jobs of ``session_id`` this machine holds, oldest first: ``{n, id, path, title, command, reported,
    started}``. A folder is a job when its name is the session's and it holds a process id (a start that never ran
    left none)."""
    base, part, out = root(home), sj.session_part(session_id), []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        got = sj.job_of_dir(name)
        if not got or got[0] != part:
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(os.path.join(path, "pid")):
            continue
        try:
            started = os.stat(os.path.join(path, "pid")).st_mtime
        except OSError:
            started = 0.0
        out.append({"n": got[1], "id": sj.job_id(got[1]), "path": path,
                    "title": _read(os.path.join(path, "title"), 400).strip(),
                    "command": _read(os.path.join(path, "cmd"), 2000).strip(),
                    "reported": _read(os.path.join(path, REPORTED), 40).strip(), "started": started})
    return sorted(out, key=lambda j: j["n"])


def find(session_id: str, ident, home: str = ""):
    """The job named ``j3`` / ``3``, or None."""
    want = str(ident or "").strip().lower().lstrip("j")
    return next((j for j in mine(session_id, home) if str(j["n"]) == want), None) if want.isdigit() else None


def poll(session_id: str, n: int, home: str = "") -> str:
    """The raw output of the job's poll line, run here -- what the kernel reads with its own reader. "" when the
    line could not run."""
    try:
        got = subprocess.run(["sh", "-c", sj.poll_line(sj.job_dir(session_id, n))], env=_env(home),
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=POLL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return got.stdout.decode("utf-8", errors="replace")


def state_of(session_id: str, n: int, home: str = "") -> dict:
    """``{state, exit_code, tail}`` of a job, read now (``sj.read_poll``); state "" when nothing could be read."""
    return sj.read_poll(poll(session_id, n, home))


def cancel(session_id: str, n: int, home: str = "") -> tuple:
    """End job ``n``: ``(sent?, the line's own words)``."""
    try:
        got = subprocess.run(["sh", "-c", sj.cancel_line(sj.job_dir(session_id, n))], env=_env(home),
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=POLL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, type(exc).__name__
    text = got.stdout.decode("utf-8", errors="replace")
    return got.returncode == 0 and "cancel sent" in text, text.strip()


def mark_reported(path: str, state: str) -> None:
    try:
        with open(os.path.join(path, REPORTED), "w", encoding="utf-8") as f:
            f.write(str(state or "ended"))
    except OSError:
        pass


def log_tail(path: str, max_bytes: int = 16000) -> str:
    """The end of a job's log, whole lines where it was cut."""
    try:
        with open(os.path.join(path, "log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    return text.split("\n", 1)[-1] if size > max_bytes and "\n" in text else text


def line_of(job: dict, got: dict, now: float = 0.0) -> str:
    """One job as the terminal lists it: its id, its state read now, how long, what it is."""
    state, code = str(got.get("state") or "unreadable"), got.get("exit_code")
    how = {sj.RUNNING: "running", sj.DONE: "done (exit 0)", sj.FAILED: f"failed (exit {code})",
           sj.CANCELLED: "cancelled", sj.LOST: "lost (its process is gone and left no exit code)"}.get(state, state)
    if state == sj.RUNNING and job.get("started"):
        secs = max(0, int((now or time.time()) - float(job["started"])))
        how += f" for {secs // 60} min {secs % 60} s" if secs >= 60 else f" for {secs} s"
    what = job.get("title") or job.get("command") or ""
    cmd = job.get("command") or ""
    return f"{job['id']}  {how}  {what}" + (f"  `{cmd}`" if cmd and cmd != what else "")


def prune(home: str = "", now: float = 0.0, keep_days: float = KEEP_DAYS) -> int:
    """Remove what nobody will read again: a job's folder whose end the kernel holds and that is older than
    ``keep_days``, and a run folder no job leads to whose process is gone (a terminal that died mid-command).
    Returns how many folders went. Never touches a running process's folder."""
    base, now, gone = root(home), now or time.time(), 0
    runs = os.path.join(base, sj.RUNS)
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    linked = set()
    for name in names:
        path = os.path.join(base, name)
        if name == sj.RUNS or not sj.job_of_dir(name):
            continue
        target = os.path.realpath(path)
        old = False
        try:
            old = now - os.stat(os.path.join(target, "pid")).st_mtime > keep_days * 86400
        except OSError:
            old = os.path.islink(path)                    # a link to nothing: its run folder is gone
        if old and (os.path.isfile(os.path.join(target, REPORTED)) or not os.path.isdir(target)):
            if os.path.islink(path):
                os.unlink(path)
                if os.path.dirname(target) == os.path.realpath(runs):
                    shutil.rmtree(target, ignore_errors=True)
            else:
                shutil.rmtree(path, ignore_errors=True)
            gone += 1
        elif os.path.islink(path):
            linked.add(target)
    try:
        held = os.listdir(runs)
    except OSError:
        return gone
    for name in held:
        path = os.path.join(runs, name)
        # only a real folder of the terminal's own is ever removed: a link put here leads somewhere this module
        # did not make (security read 2026-11-27)
        if os.path.islink(path) or not os.path.isdir(path) or os.path.realpath(path) in linked:
            continue
        try:
            pid = int(_read(os.path.join(path, "pid"), 20).strip() or 0)
            fresh = now - os.stat(path).st_mtime < 3600
        except (OSError, ValueError):
            continue
        if fresh or _alive(pid):
            continue
        shutil.rmtree(path, ignore_errors=True)
        gone += 1
    return gone


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    for target in (-pid, pid):                # the group, or the process itself
        try:
            os.kill(target, 0)
            return True
        except ProcessLookupError:
            continue
        except OSError:
            return True                       # it is there and not ours to signal
    return False
