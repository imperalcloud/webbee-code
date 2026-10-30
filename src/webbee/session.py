import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict, deque

from webbee.coding_context import build_coding_context, detect_verify_cmd
from webbee.http import make_client
from webbee.consent import _retire, race_consent
from webbee.frames import (
    _MARATHON_FACT_TYPES,
    _first_time,
    _is_foreign_frame,
    _origin_tag,
    _progress_text,
    _summary,
    handle_action_frame,
    handle_step_finished,
    handle_step_started,
    handle_tool_request,
    marathon_note,
    render_foreign_frame,
    render_todo_frame,
)


# W1 Task 10 (own-task watchdog): a test seam for the deduped-twin-hang clock
# below -- monkeypatched as `webbee.session._monotonic` so a test can drive
# elapsed-time without a real sleep.
_monotonic = time.monotonic

# Deduped-twin hang (W1 front-3b): a turn's task_id can be ring-dropped
# kernel-side as an at-least-once twin, so the shared stream carries ONLY
# frames tagged with OTHER task_ids while this turn's own task_id never
# appears. Left alone the dock spins "working" forever. Past this many
# seconds of foreign-only traffic with zero own frames, end the turn honestly.
_FOREIGN_ONLY_DEADLINE_S = 90.0


_SEEN_KEEP = 64   # kernel re-dispatch only ever targets the CURRENT pending
                  # req_id -- a small recency window is enough; unbounded full
                  # results (file bodies, bash output) were the many-hour-
                  # marathon RAM leak (W1 front-1).


# Kernel silence on THIS turn (2026-10-30 review M7): the dock showed
# "working" forever when the kernel workflow died -- nothing on the stream ever
# says so. Past the first bound the user is told (and the wait goes on: a long
# brain step is legal); past the second the turn ends honestly, the same way
# the foreign-only deadline above ends it. A request the kernel still sends
# later is picked up by the idle attach poll. Only the turn's OWN frames (and
# our own result posts) count as activity; a local tool run or a consent
# prompt is never timed.
_KERNEL_QUIET_NOTE_S = 10 * 60.0
_KERNEL_QUIET_END_S = 30 * 60.0

# The result channel's size bound (2026-10-30 review H5): a view_image result
# carries the whole image as base64, and a big one came back 413 -- the old
# code took any 4xx as delivered, so the kernel waited out its tool-wait
# expiry (up to ~65 min). A result is cut to this before it is posted, with an
# honest note of what was cut.
_RESULT_MAX_BYTES = 4 * 1024 * 1024

# Executed-result record on disk (2026-10-30 review H6): attach() makes the
# kernel re-send the pending request, and the in-memory `seen` map is new for
# every _serve_stream -- so a crash/kill between running a tool and posting its
# result ran it AGAIN (bash twice, an edit applied twice). Each executed
# tool_request's result is written here BEFORE it is posted; a req_id found
# here re-posts the stored result instead of running again.
_RESULT_CACHE_DIR = os.path.expanduser("~/.cache/webbee/results")   # test seam: monkeypatch this name
_RESULT_KEEP = 64                       # per session -- the same recency window as _SEEN_KEEP
_RESULT_STORE_MAX_BYTES = 1024 * 1024   # one stored result; a bigger one is kept cut (it ran once already)
_RESULT_STORE_TTL_S = 24 * 3600.0       # the server keeps 24h of stream -- an older record is never re-sent
_RESULT_DIR_TTL_S = 7 * 24 * 3600.0     # whole session folders untouched this long are removed


def _json_bytes(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def _cut_text(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max(0, max_bytes)].decode("utf-8", errors="ignore")


def _cap_result(out: dict, limit: int = _RESULT_MAX_BYTES) -> dict:
    """`out` ({"req_id", "result"}) cut to at most `limit` bytes of JSON. A
    result that fits is returned unchanged. Otherwise big non-text fields (a
    view_image `image_data`) are dropped and named, then `content` is cut with
    a note of how much was sent; if even that does not fit, only ok + the cut
    content go. Never raises."""
    try:
        total = _json_bytes(out)
    except Exception:
        return out
    if total <= limit:
        return out
    res = out.get("result")
    res = dict(res) if isinstance(res, dict) else {"ok": False, "content": str(res)}
    notes = []
    for key in [k for k, v in res.items() if k != "content" and isinstance(v, str) and len(v) > 64 * 1024]:
        size = len(res.pop(key))
        if key == "image_data":
            res["native_vision"] = False
            notes.append(f"[image not sent: {size} bytes of image data is over the "
                         f"{limit // (1024 * 1024)} MB a result can carry -- shrink or crop it and view it again]")
        else:
            notes.append(f"[{key} not sent: {size} bytes]")
    content = str(res.get("content", ""))
    note = "\n".join(notes)
    capped = {"req_id": out.get("req_id"), "result": {**res, "content": content + ("\n" + note if note else "")}}
    if _json_bytes(capped) <= limit:
        return capped
    content_bytes = len(content.encode("utf-8"))
    cut_note = (f"\n… [result cut: {content_bytes} bytes of output, only the start is sent -- "
                f"a result can carry at most {limit // (1024 * 1024)} MB]")
    rest = {**res, "content": ""}
    budget = limit - _json_bytes({"req_id": out.get("req_id"), "result": rest}) - _json_bytes(note + cut_note) - 1024
    if budget < limit // 2:     # other big fields (a huge diff) -- send only ok + the cut text
        rest = {"ok": bool(res.get("ok"))}
        budget = limit - 4096 - _json_bytes(note + cut_note)
    # JSON escaping can grow text (quotes, control chars): halve until it fits
    while True:
        head = _cut_text(content, budget)
        capped = {"req_id": out.get("req_id"),
                  "result": {**rest, "content": head + ("\n" + note if note else "") + cut_note}}
        if _json_bytes(capped) <= limit or budget <= 0:
            return capped
        budget //= 2


class _ResultStore:
    """On-disk record of this session's executed tool_request results, one
    small JSON file per req_id under _RESULT_CACHE_DIR/<session>. Keyed by
    task_id + req_id (a req_id's counter can restart in a new kernel run; a
    turn's task_id cannot repeat) and checked against the tool name and age, so
    a stale record is never mistaken for this request. A write-tier tool is
    marked `started` before it runs: a crash during the run leaves the mark,
    and the re-sent request answers "outcome unknown, not run again" instead
    of applying the change twice. Fail-soft both ways: an unreadable or
    unwritable cache is exactly today's behaviour (the tool runs)."""

    def __init__(self, session_id: str, task_id: str) -> None:
        self.task_id = str(task_id or "")
        self.enabled = bool(session_id and self.task_id)
        h = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
        self.dir = os.path.join(_RESULT_CACHE_DIR, h)

    def _path(self, rid) -> str:
        h = hashlib.sha256(f"{self.task_id}\0{rid}".encode("utf-8")).hexdigest()[:24]
        return os.path.join(self.dir, h + ".json")

    def get(self, rid, tool: str):
        """The stored {"req_id", "result"} for `rid`, or None (not run here)."""
        if not self.enabled or not rid:
            return None
        try:
            with open(self._path(rid), encoding="utf-8") as f:
                rec = json.load(f)
            if (rec.get("task_id") != self.task_id or rec.get("req_id") != rid
                    or rec.get("tool") != tool or time.time() - float(rec.get("at", 0)) > _RESULT_STORE_TTL_S):
                return None
            if rec.get("state") == "started":
                return {"req_id": rid, "result": {
                    "ok": False,
                    "content": (f"{tool} was started before webbee stopped and its outcome is unknown -- "
                                "not run again; check the workspace (git status, read the file) before retrying")}}
            out = rec.get("out")
            return out if isinstance(out, dict) else None
        except Exception:
            return None

    def mark_started(self, rid, tool: str) -> None:
        self._write(rid, {"state": "started", "tool": tool})

    def put(self, rid, tool: str, out: dict) -> None:
        self._write(rid, {"state": "done", "tool": tool, "out": _cap_result(out, _RESULT_STORE_MAX_BYTES)})

    def _write(self, rid, rec: dict) -> None:
        if not self.enabled or not rid:
            return
        try:
            first = not os.path.isdir(self.dir)
            os.makedirs(self.dir, exist_ok=True)
            path = self._path(rid)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({**rec, "task_id": self.task_id, "req_id": rid, "at": time.time()}, f,
                          ensure_ascii=False, default=str)
            os.replace(tmp, path)                     # atomic: a crash mid-write never leaves half a record
            self._prune()
            if first:
                self._prune_old_sessions()
        except Exception:
            pass

    def _prune(self) -> None:
        names = [n for n in os.listdir(self.dir) if n.endswith(".json")]
        if len(names) <= _RESULT_KEEP:
            return
        paths = sorted((os.path.join(self.dir, n) for n in names), key=os.path.getmtime)
        for p in paths[:len(paths) - _RESULT_KEEP]:
            try:
                os.remove(p)
            except OSError:
                pass

    def _prune_old_sessions(self) -> None:
        import shutil
        now = time.time()
        for n in os.listdir(_RESULT_CACHE_DIR):
            p = os.path.join(_RESULT_CACHE_DIR, n)
            try:
                if p != self.dir and os.path.isdir(p) and now - os.path.getmtime(p) > _RESULT_DIR_TTL_S:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass


def _cancel_running(executor) -> None:
    """Kill the commands the executor still runs in its worker thread (Esc /
    stop, 2026-10-30 review H3): cancelling the asyncio task leaves the thread
    -- and its subprocess, up to 3600 s -- running. `cancel_running` is
    optional (an older or fake executor lacks it); never raises."""
    fn = getattr(executor, "cancel_running", None)
    if fn is None:
        return
    try:
        fn()
    except Exception:
        pass


def _remember(seen: OrderedDict, rid, out) -> None:
    """LRU-bounded store for `seen` -- the run() dedup cache. A (re-)store
    always counts as the most-recent touch; the least-recently-touched entry
    is evicted once the store grows past _SEEN_KEEP."""
    seen[rid] = out
    seen.move_to_end(rid)
    while len(seen) > _SEEN_KEEP:
        seen.popitem(last=False)


def _is_transient_status(status: int) -> bool:
    return status >= 500 or status in (408, 429)


def _transient_exceptions():
    # Lazy import (module-level `import httpx` would move httpx's cost to CLI
    # boot -- session.py is imported by repl.py at module top, and the rest of
    # this module already keeps httpx as a run()-local, boot-time-optimization
    # import). `except <call expression>` is valid Python -- the call just
    # has to happen on every except test, which is cheap (import is memoized).
    import httpx
    return (httpx.HTTPError, OSError)


async def _transient_retry(send, *, attempts: int = 5, base: float = 1.0, cap: float = 8.0):
    """Bounded transient-retry for gateway WRITE calls (turn-start POST): a
    502 during a deploy must not kill the turn. Verdict/normal statuses return
    immediately; TRANSPORT errors (httpx.HTTPError/OSError) and 5xx/408/429
    retry with capped backoff -- anything else (a bug in the caller) is not a
    transient condition and must surface immediately, not be retried 5x.
    The LAST failure is returned/raised for the caller's raise_for_status."""
    backoff = base
    last_exc = None
    last_resp = None
    n = max(1, attempts)
    for i in range(n):
        try:
            resp = await send()
            if not _is_transient_status(resp.status_code):
                return resp
            last_resp, last_exc = resp, None
        except _transient_exceptions() as e:
            last_exc, last_resp = e, None
        if i < n - 1:               # never sleep after the FINAL attempt
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, cap)
    if last_exc is not None:
        raise last_exc
    return last_resp


class AgentSession:
    """Client-side driver for one coding turn against the Imperal cloud.
    The brain runs server-side; this is the hands — it streams kernel-
    pre-approved tool_request frames over SSE, runs each tool locally, relays
    confirm_request replies RAW for the brain to interpret, drives the sink
    for live UI, and posts results back until a final frame arrives.

    P1: one POST per turn (server reloads the shared webbee-terminal thread,
    so context carries across turns). Persistent signal-based sessions are P3."""

    def __init__(self, cfg, token_provider, workspace_root: str, mode: str = "default", intel=None,
                 shadow=None, slot_id: str = "", model_tier: str = "") -> None:
        self.cfg = cfg
        self.token_provider = token_provider
        self.workspace_root = workspace_root
        self.mode = mode
        # webbee-code-model-tier-slash-command-v1: "" = server admin default
        # (client never chose one); else "webbeesmart"|"supersmart"|"ultrasmart".
        # Mutated in place by repl.set_slot_tier, exactly like self.mode is
        # mutated by set_slot_mode -- read fresh into coding_context on every
        # turn below, so a mid-session /model change takes effect next turn.
        self.model_tier = model_tier
        self.session_id: str = ""
        self.steps: deque = deque(maxlen=200)
        self._task_id: str = ""
        self._executor = None  # the running turn's LocalToolExecutor -- stop() kills its commands
        self._intel = intel  # IntelService, or None (base install / boot failure)
        self._shadow = shadow  # ShadowGit, or None (git unavailable / boot failure)
        # W4b T5: "" (tab-1 / the fallback loop's only slot) keeps every turn's
        # POST body byte-identical to before this feature existed -- a later
        # tab's own short slot_id rides the body as `slot`, and the gateway
        # mints an `-s{slot}`-suffixed session id from it.
        self.slot_id = slot_id

    def _file_client_factory(self):
        """Zero-arg callable -> ImperalClient, for read_file's native-document
        path (native-document-read-v1).

        A FACTORY, not a client: LocalToolExecutor runs in a worker thread with
        its own event loop, so the client must be built there, not shared from
        here. Returns None when this session has no cloud config -- the read
        then degrades to one honest sentence instead of a crash.

        Threadsafe Loop-Bridge (Python 3.6 - 3.14+):
        token_provider is bound to the main event loop's lock. We bridge it across
        thread boundaries via asyncio.run_coroutine_threadsafe so worker event loops
        never raise RuntimeError: Lock is bound to a different event loop.
        """
        cfg, token_provider = self.cfg, self.token_provider
        if cfg is None or token_provider is None:
            return None

        import asyncio

        try:
            main_loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                main_loop = asyncio.get_event_loop()
            except RuntimeError:
                main_loop = None

        def _make():
            from imperal_mcp.client import ImperalClient

            if main_loop is not None and main_loop.is_running():
                async def _bridged_token_provider() -> str:
                    try:
                        current_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        current_loop = None
                    if current_loop is main_loop:
                        return await token_provider()
                    fut = asyncio.run_coroutine_threadsafe(token_provider(), main_loop)
                    return await asyncio.wrap_future(fut)
                return ImperalClient(cfg, _bridged_token_provider)

            return ImperalClient(cfg, token_provider)

        return _make

    async def _headers(self) -> dict:
        token = await self.token_provider()
        return {"Authorization": f"Bearer {token}"}

    async def post_mode(self, mode: str) -> bool:
        """Tell the running session its new coding mode (the gateway signals the kernel's ``set_mode``): autopilot
        is asked nothing from now on, the others are asked again. False when there is no session yet or no live one
        took it -- the next turn carries the mode anyway. Never raises."""
        if not self.session_id or self.cfg is None or self.token_provider is None:
            return False
        try:
            headers = await self._headers()
            async with make_client(self.cfg, timeout=15) as client:
                resp = await client.post(f"/v1/agent/sessions/{self.session_id}/mode", json={"mode": mode},
                                         headers=headers)
            return resp.status_code == 200 and bool((resp.json() or {}).get("ok"))
        except Exception:
            return False

    async def run(self, task: str, sink, *, marathon: bool = False, goal: str = "",
                  surface: str = "", steer_iid: str = "") -> str:
        import httpx
        from uuid import uuid4

        from webbee.tools import LocalToolExecutor
        from imperal_mcp.client import ImperalClient

        # FIX1 (W1 final review): every turn-start POST needs a stable dedup
        # key. A typed turn mints its own (steer_iid arrives empty); a steer
        # pickup keeps the queue entry's OWN id. Either way, _transient_retry
        # below re-sends this SAME closure -- so a retry after an ambiguous
        # failure (timeout / edge-504 whose first attempt may have already
        # landed) carries the IDENTICAL key, and the kernel's steer-iid ring
        # drops the twin instead of executing the instruction twice.
        steer_iid = steer_iid or uuid4().hex

        # Offload to a worker thread — build_coding_context runs sync
        # subprocess.run(git status, timeout=10) + os.walk; inline on the dock's
        # asyncio loop it froze the whole UI at every turn start.
        coding_context = await asyncio.to_thread(build_coding_context, self.workspace_root, self._intel)
        if self.model_tier:
            coding_context = {**coding_context, "model_tier": self.model_tier}
        # the mode this turn runs under: in autopilot the kernel asks no consent at all (no card, no park); a switch
        # mid-turn reaches the running session through post_mode
        coding_context = {**coding_context, "mode": str(self.mode or "default")}
        if marathon:
            # verify_cmd is CLIENT-detected here and carried in coding_context —
            # the trusted proof-of-done the kernel runs (never brain-authored).
            root = coding_context.get("repo_root") or coding_context.get("cwd") or self.workspace_root
            verify_cmd = await asyncio.to_thread(detect_verify_cmd, root)
            coding_context = {**coding_context, "verify_cmd": verify_cmd}
        imperal_id = await ImperalClient(self.cfg, self.token_provider).whoami()
        executor = LocalToolExecutor(self.workspace_root, indexer=self._intel,
                                     shadow=self._shadow,
                                     client_factory=self._file_client_factory())

        body = {"user_id": imperal_id, "task": task, "coding_context": coding_context}
        if surface:
            # Liveness v2 §B: an idle-steer pickup carries the queued item's
            # origin surface so the kernel adopts it start-path (provenance +
            # [surface] tags). Additive-only -- a typed turn's body is
            # byte-identical to before.
            body["surface"] = surface
        # steer-iid-dedup: ALWAYS present now (FIX1 above) -- W1 turn-start
        # retries need the ring dedup, so a typed turn's minted id rides the
        # body exactly like a pickup's own id. No longer additive-only for
        # this key (surface/marathon/goal above still are).
        body["steer_iid"] = steer_iid
        if self.slot_id:
            # W4b T5: slot-suffixed session ids -- the gateway mints
            # {prefix}-{imperal_id}-r{repo_key}-s{slot} when a slot rides the
            # body. Additive-only: "" (tab-1 / fallback's only slot) keeps
            # today's body byte-identical, no "slot" key at all.
            body["slot"] = self.slot_id
        if marathon:
            body["marathon"] = True
            body["goal"] = goal

        headers = await self._headers()
        async with make_client(self.cfg, timeout=60) as client:     # keepalive: a slept socket dies, never hangs
            resp = await _transient_retry(lambda: client.post(
                "/v1/agent/sessions", json=body, headers=headers))
            resp.raise_for_status()
            _sess = resp.json()
            session_id = _sess["session_id"]
            start_id = _sess.get("last_id", "0-0")
            task_id = _sess.get("task_id", "")
            return await self._serve_stream(sink, client, session_id, start_id, task_id,
                                            executor, marathon=marathon)

    async def attach(self, sink, *, task_id: str, start_id: str, marathon: bool = True) -> str:
        """Attach-on-poll (idle terminal picks up a kernel-dispatched task):
        NO start POST. A marathon turn woken by a panel/Telegram message
        while this terminal sat idle can dispatch a local tool_request/
        confirm_request that nothing attaches -- the gateway's
        `GET .../pending-steer` response flags this via its additive
        `attach` field (set only when the drain found no items AND the
        session's stream tail holds an unanswered request) and hands the
        client `task_id`/`last_id` to resume from. This session id is
        DERIVED the same way `webbee.steer.derive_session_id` derives it
        for polling in the first place (this AgentSession already knows its
        own workspace + slot_id, so the derivation is reused verbatim, not
        reinvented) -- simply opening the SSE stream against it fires
        `client_connected` server-side, which makes the kernel re-dispatch
        the pending request onto the stream. From there this runs through
        the EXACT SAME frame-loop `run()` uses (`_serve_stream`): tool
        execution, consent racing, dedup, and the final/marathon_paused
        exits all behave identically to a normal turn -- including
        begin_turn/end_turn semantics, busy gates, and Esc/stop (both
        driven by the caller around this call, same as any other turn)."""
        import httpx

        from webbee.steer import derive_session_id
        from webbee.tools import LocalToolExecutor

        session_id = await derive_session_id(
            self.cfg, self.token_provider, self.workspace_root,
            marathon=marathon, slot_id=self.slot_id)
        executor = LocalToolExecutor(self.workspace_root, indexer=self._intel,
                                     shadow=self._shadow,
                                     client_factory=self._file_client_factory())
        async with make_client(self.cfg, timeout=60) as client:     # keepalive: a slept socket dies, never hangs
            return await self._serve_stream(sink, client, session_id, start_id, task_id,
                                            executor, marathon=marathon)

    async def _serve_stream(self, sink, client, session_id: str, start_id: str,
                            task_id: str, executor, *, marathon: bool = False) -> str:
        """The frame-loop phase of ONE coding turn -- factored out of `run()`
        (behavior-preserving split) so `attach()` (no start POST at all --
        see there) can drive the identical loop against a session id it
        derived itself instead of one a fresh POST just minted. Sets
        `self.session_id`/`self._task_id` itself (rather than trusting the
        caller already did) so every frame-loop reference to `self._task_id`
        below (the C7 foreign-frame filter, the consent race) sees the
        RIGHT turn's id regardless of which caller reached this point."""
        self.session_id = session_id
        self._task_id = task_id
        self.steps = deque(maxlen=200)

        seen: OrderedDict = OrderedDict()  # req_id -> already-posted result
                                            # (at-least-once dedup); LRU-64 via
                                            # _remember -- see _SEEN_KEEP.
        # Slice-5 T9: id-sets shared across BOTH vocabularies for EXT tools
        # (the kernel reuses the SAME tc["id"] as step_id there), so a step
        # announced by one vocabulary is never re-announced by its
        # dual-emitted twin. step_labels carries a v2 step_started's label
        # forward to its later step_finished (which carries no app_id/tool
        # of its own — facts-only). local_ids tracks LOCAL-tool v2 step_ids,
        # which use a disjoint id space from the tool_request round trip
        # (see webbee.frames' module docstring) and are a pure no-op.
        started: set = set()
        finished: set = set()
        step_labels: dict = {}
        local_ids: set = set()
        from webbee.stream import stream_frames
        _fr = getattr(self.token_provider, "force_refresh", None)
        _rc = getattr(sink, "reconnecting", None)
        stream = stream_frames(client, session_id, self._headers, start_id=start_id,
                               force_refresh=_fr, on_retry=_rc)
        # Liveness A: explicit __anext__ pulls (not `async for`) so a
        # pending local consent can RACE the stream. Between iterations at
        # most ONE of carry_task/carry_frame is set — a consent race hands
        # ownership of its pulled-ahead pull back to this loop: a pending
        # task when consent won, an already-pulled frame when the stream
        # won. Everything else is byte-identical to the old async-for.
        carry_task = None    # a still-pending __anext__ task (consent won)
        carry_frame = None   # an already-pulled frame (the stream won)
        _t0 = _monotonic()   # W1 Task 10: own-task watchdog start
        _own_frames = False  # -> True the instant ANY non-foreign frame lands
        from webbee.tools import _WRITE_TIER
        self._executor = executor           # stop() kills what it still runs
        store = _ResultStore(session_id, task_id)
        loop = asyncio.get_running_loop()
        _last_own = loop.time()             # M7: the turn's own last activity
        _quiet_noted = False
        try:
            while True:
                if carry_frame is not None:
                    frame, carry_frame = carry_frame, None
                else:
                    if carry_task is None:
                        carry_task = asyncio.ensure_future(stream.__anext__())
                    # The pending pull stays in carry_task while it is awaited,
                    # so the finally below retires it on any exit (Esc, the
                    # silence deadline) -- asyncio.wait never cancels it.
                    quiet = loop.time() - _last_own
                    bound = _KERNEL_QUIET_END_S if _quiet_noted else _KERNEL_QUIET_NOTE_S
                    done, _ = await asyncio.wait({carry_task}, timeout=max(0.0, bound - quiet))
                    if not done:
                        quiet = loop.time() - _last_own
                        if quiet >= _KERNEL_QUIET_END_S:
                            _note = getattr(sink, "note", None)
                            if _note is not None:
                                _note(f"⚠ no activity from the kernel for {int(quiet // 60)} min — this turn is "
                                      "ended here; anything the kernel still sends is picked up when the "
                                      "terminal is idle")
                            return ""
                        if not _quiet_noted and quiet >= _KERNEL_QUIET_NOTE_S:
                            _quiet_noted = True
                            _note = getattr(sink, "note", None)
                            if _note is not None:
                                _note(f"⏳ no activity from the kernel for {int(quiet // 60)} min — "
                                      "still waiting (Esc stops the turn)")
                        continue
                    pull, carry_task = carry_task, None
                    try:
                        frame = pull.result()
                    except StopAsyncIteration:
                        break
                ftype = frame.get("type")

                # A frame from a DIFFERENT turn on the shared persistent stream
                # (task_id absent on legacy kernels -> treated as own). C7 safety:
                # foreign actionable frames are NEVER executed/consented and NEVER
                # end this turn -- but instead of vanishing they (and any origin-
                # stamped cross-surface display frame, e.g. a Telegram-steered
                # turn's progress) now render ONE tagged, display-only line.
                if _is_foreign_frame(frame, self._task_id):
                    render_foreign_frame(frame, sink)
                    if (not _own_frames and self._task_id
                            and _monotonic() - _t0 > _FOREIGN_ONLY_DEADLINE_S):
                        # Deduped-twin hang (W1 front-3b): the kernel ring
                        # dropped this turn's task as a twin -- its task_id
                        # will never appear on the stream. Foreign traffic
                        # flowing + zero own frames is the signature; end
                        # honestly instead of spinning "working" forever.
                        _note = getattr(sink, "note", None)
                        if _note is not None:
                            _note("⚠ this message produced no work of its own "
                                  "(likely a duplicate the kernel dropped) — done waiting")
                        return ""
                    continue
                _own_frames = True
                _last_own, _quiet_noted = loop.time(), False

                # Live steer topology: a Telegram/panel-steered turn keeps THIS
                # client's task_id (the terminal stays the sole executor) with
                # `origin` stamped -- tag the text renders below; everything
                # else (execution, dedup, consent, accounting) is unchanged.
                _tag = _origin_tag(frame)

                if ftype == "tool_request":
                    rid = frame.get("req_id")
                    sid = str(rid or "")
                    tool = str(frame.get("tool", "") or "")
                    stored = None if rid in seen else await asyncio.to_thread(store.get, rid, tool)
                    if rid in seen:
                        seen.move_to_end(rid)
                        out = seen[rid]
                    elif stored is not None:
                        # H6: ran before this client restarted (or was killed
                        # mid-run) -- the kernel re-sent it on reconnect; never
                        # run it twice.
                        out = stored
                        _remember(seen, rid, out)
                        _note = getattr(sink, "note", None)
                        if _note is not None:
                            try:
                                _note(f"↺ {tool} already ran before the reconnect — its result is sent again, "
                                      "not run twice")
                            except Exception:
                                pass
                    elif self.mode == "plan" and tool in _WRITE_TIER:
                        # M3: plan mode is read-only on THIS side too -- the
                        # kernel's consent gate is the first wall, this is the
                        # second (a tool_request is otherwise run unconditionally).
                        out = {"req_id": rid, "result": {
                            "ok": False,
                            "content": f"plan mode: {tool} changes files/runs commands -- not run"}}
                        _pb = getattr(sink, "plan_blocked", None)
                        if _pb is not None:
                            try:
                                _pb(tool)
                            except Exception:
                                pass
                        _remember(seen, rid, out)
                    else:
                        # UI rendering is guarded so it can never block the
                        # result POST below (an unposted result hangs the kernel
                        # dispatch and freezes the dock).
                        if _first_time(sid, started):
                            try:
                                sink.tool_start(_tag + frame.get("tool", ""), frame.get("args", {}))
                            except Exception:
                                pass
                        if tool in _WRITE_TIER:
                            await asyncio.to_thread(store.mark_started, rid, tool)
                        try:
                            out = await asyncio.to_thread(handle_tool_request, frame, executor)
                        except asyncio.CancelledError:
                            # H3: Esc cancels this await, not the worker thread --
                            # kill the command it runs, then let the cancel through.
                            _cancel_running(executor)
                            raise
                        out = _cap_result(out)
                        # H6: written BEFORE the post -- a crash after this line
                        # re-sends the result instead of running the tool again.
                        await asyncio.to_thread(store.put, rid, tool, out)
                        res = out["result"]
                        if _first_time(sid, finished):
                            try:
                                sink.tool_result(_tag + frame.get("tool", ""), bool(res.get("ok")), _summary(res))
                                # 0.3.40 (I-STREAM-STEP-DIFF): local write_file/
                                # edit_file/multi_edit hand back a unified diff
                                # of what actually changed on disk -- shown
                                # right after the result line via an OPTIONAL
                                # sink hook (getattr-guarded like `thinking`/
                                # `reconnecting` above: a plain/older sink that
                                # doesn't implement it just skips the extra
                                # detail, never breaks).
                                _diff = res.get("diff") if isinstance(res, dict) else ""
                                if _diff:
                                    _td = getattr(sink, "tool_diff", None)
                                    if _td is not None:
                                        _td(frame.get("tool", ""), _diff)
                                self.steps.append({"step_id": sid,
                                                   "label": frame.get("tool", ""),
                                                   "ok": bool(res.get("ok"))})
                            except Exception:
                                pass
                        _remember(seen, rid, out)
                    await self._post_result(client, session_id, out, sink=sink, tool=tool)
                    _last_own, _quiet_noted = loop.time(), False

                elif ftype == "confirm_request":
                    rid = frame.get("req_id")
                    if rid in seen:
                        seen.move_to_end(rid)
                        await self._post_result(client, session_id, seen[rid], sink=sink)
                    else:
                        if self.mode == "plan":
                            sink.plan_blocked(frame.get("tool", ""))
                        # Liveness A: the local prompt races the stream so a
                        # consent answered from ANOTHER surface (Telegram
                        # relay) unfreezes this terminal instead of leaving
                        # the dock stuck on `approve? y/n`.
                        race = await race_consent(frame, sink, stream,
                                                  mode=self.mode, task_id=self._task_id)
                        carry_task, carry_frame = race.carry_task, race.carry_frame
                        if race.stream_ended:
                            break
                        if race.out is not None:
                            _remember(seen, rid, race.out)
                            await self._post_result(client, session_id, race.out, sink=sink)
                        _last_own, _quiet_noted = loop.time(), False   # a consent wait is the user's, not the kernel's

                elif ftype == "panel_release_required":
                    sink.panel_release(frame.get("panel_url", ""), frame.get("summary", ""))

                elif ftype == "action":  # R2 — ext-tool call (server-side) surfaced in the feed
                    handle_action_frame(frame, sink, started, finished, self.steps)

                elif ftype == "step_started":  # v2 (Slice-5 T8 dual-emit)
                    handle_step_started(frame, sink, started, step_labels, local_ids)

                elif ftype == "step_finished":  # v2 (Slice-5 T8 dual-emit)
                    handle_step_finished(frame, sink, finished, step_labels, self.steps, local_ids)

                elif ftype == "thinking":  # system-driven reasoning -> the 💭 block
                    _text = _progress_text(frame)
                    thinking = getattr(sink, "thinking", None)
                    if thinking is not None:
                        # The selected tier belongs to THIS running AgentSession.
                        # Passing it here keeps the visible reasoning colour in
                        # lock-step with the model that produced the frame instead
                        # of letting the renderer fall back to Smart's colour.
                        try:
                            thinking(_tag + _text if _text else "", tier=self.model_tier)
                        except TypeError:
                            thinking(_tag + _text if _text else "")
                    else:
                        sink.progress(_tag + _text if _text else "")

                elif ftype == "progress":  # P2 — dual-reads llm_text (v2) / text (legacy)
                    _text = _progress_text(frame)
                    sink.progress(_tag + _text if _text else "")

                elif ftype == "usage":  # P2 — cumulative tokens + credits (Slice C; raw $ stays server-side)
                    sink.usage(
                        int(frame.get("tokens", 0) or 0),
                        int(frame.get("credits", 0) or 0),
                    )

                elif ftype in ("task_queued", "task_dequeued"):
                    # Full-queue-layer K1: a follow-up queued into the RUNNING
                    # kernel session shows in the live queue panel the instant
                    # it queues (tagged by origin) and leaves the panel when
                    # the kernel drains it. These frames carry NO task_id
                    # (they belong to the session, not a turn), so the C7
                    # filter never eats them; getattr-guarded like todos/
                    # queued_run — a minimal sink drops them, a render error
                    # never breaks the loop. Terminal-origin rows render TOO
                    # (mid-turn inject, 0.3.15): an injected line never sits
                    # in the LOCAL panel — the kernel's echo is its only row,
                    # and task_dequeued clears it when the turn absorbs it.
                    _origin = str(frame.get("origin", "") or "")
                    _hook = getattr(sink, "remote_queued" if ftype == "task_queued"
                                    else "remote_dequeued", None)
                    if _hook is not None:
                        _iid = str(frame.get("steer_iid", "") or "")
                        try:
                            if ftype == "task_queued":
                                _hook(_origin, str(frame.get("text", "") or ""), _iid)
                            else:
                                _hook(_origin, _iid)
                        except Exception:
                            pass

                elif ftype == "marathon_complete":  # U4 — the whole GOAL is done: terminal
                    return frame.get("text", "")

                elif ftype in _MARATHON_FACT_TYPES:  # U4 — marathon plan/milestone/pause/todo
                    # Facts-only. A `todo` fact carries the FULL list -> the
                    # dedicated full-checklist render (falls back to the old
                    # one-line note on a minimal sink). The other facts render
                    # ONE human-readable line. Guarded: a minimal sink (no
                    # `note`) simply drops the fact rather than crashing the
                    # turn (the stream reader already tolerates unknown frame
                    # types by ignoring them).
                    if ftype == "todo":
                        render_todo_frame(frame, sink)
                    else:
                        _note = getattr(sink, "note", None)
                        if _note is not None:
                            _note(marathon_note(frame))
                    if ftype == "marathon_paused":
                        # Parked (out-of-credits / consent / runaway) -> end the
                        # turn so the dock leaves "working"; the note shows why, and
                        # the run resumes on the user's next reply. The kernel's task
                        # queue stays alive server-side while parked -- tell the sink
                        # via marathon_parked() so end_turn keeps the queue-panel's
                        # remote rows (tagged parked) instead of wiping them (W1
                        # front-3b: an empty panel over a non-empty kernel queue lies).
                        # getattr-guarded like every other sink hook here: a minimal
                        # sink drops it, a crashing hook never breaks the turn.
                        _parked = getattr(sink, "marathon_parked", None)
                        if _parked is not None:
                            try:
                                _parked(str(frame.get("reason", "") or ""))
                            except Exception:
                                pass
                        return ""

                elif ftype == "final":
                    # In a MARATHON a `final` is a PER-MILESTONE result, NOT the end
                    # of the run -> keep streaming (the goal ends on marathon_complete
                    # / marathon_paused, or a user-stop `final` with stopped=true). A
                    # non-marathon coding turn's `final` is terminal (unchanged).
                    if marathon and not frame.get("stopped"):
                        continue
                    # I-VISIBLE-ASSISTANT-QUESTION: stash the kernel's own
                    # awaits_reply fact on the sink itself (run()'s return
                    # contract is a bare str -- widening it here would touch
                    # every caller). end_turn() reads + clears it, so a sink
                    # with no support for the field (a bare test double)
                    # just never sees the attribute -- harmless getattr.
                    setattr(sink, "_awaits_reply", bool(frame.get("awaits_reply")))
                    _text = frame.get("text", "")
                    return _tag + _text if _text else ""
        finally:
            # Never leak a pulled-ahead __anext__ past the loop (e.g. an
            # exit while a consent race's carry is still pending).
            if carry_task is not None:
                await _retire(carry_task)

        return ""

    _result_delays = (0.5, 2.0, 5.0)   # class attr — tests override per instance

    async def _post_result(self, client, session_id: str, out: dict, *, sink=None, tool: str = "") -> bool:
        """POST one result under its req_id; True once a 2xx took it. Never
        raises into the stream loop. W1: bounded transient retries (5xx/408/
        429, transport errors) -- the kernel dedups by req_id, so a duplicate
        post is safe. 2026-10-30 review H5: only a 2xx counts as delivered (a
        413/400/422 used to count too, and the kernel then waited out its
        tool-wait expiry, up to ~65 min). A 401 refreshes the token once and
        posts again; any other refusal posts a small {"ok": false} note under
        the SAME req_id so the kernel moves on with the reason. A result that
        could not be delivered at all is told to the sink."""
        url = f"/v1/agent/sessions/{session_id}/result"
        refreshed = False
        last = ""
        verdict = False       # the gateway answered with a non-retryable status
        delays = list((0.0,) + tuple(self._result_delays))
        while delays:
            delay = delays.pop(0)
            if delay:
                await asyncio.sleep(delay)
            try:
                headers = await self._headers()
                r = await client.post(url, json=out, headers=headers)
            except Exception as e:
                last, verdict = type(e).__name__, False
                continue
            status = int(getattr(r, "status_code", 0) or 0)
            if 200 <= status < 300:
                return True
            last = f"{status} {getattr(r, 'reason_phrase', '') or ''}".strip()
            if status == 401 and not refreshed:
                refreshed = True
                _fr = getattr(self.token_provider, "force_refresh", None)
                if _fr is not None:
                    try:
                        await _fr()
                    except Exception:
                        pass
                delays.insert(0, 0.0)          # post again at once with the new token
                continue
            if _is_transient_status(status):
                verdict = False
                continue
            verdict = True
            break
        size = 0
        try:
            size = _json_bytes(out)
        except Exception:
            pass
        rid = out.get("req_id") if isinstance(out, dict) else None
        if verdict and rid is not None:
            # The gateway refused THIS body (too large, malformed, expired
            # auth that survived a refresh): a small note still unblocks the
            # kernel, with the reason, instead of a silent wait.
            note = {"req_id": rid, "result": {
                "ok": False, "content": f"result could not be delivered: {last}, {size} bytes"}}
            try:
                headers = await self._headers()
                r = await client.post(url, json=note, headers=headers)
                if 200 <= int(getattr(r, "status_code", 0) or 0) < 300:
                    self._tell(sink, f"⚠ the result of {tool or 'a tool'} could not be delivered ({last}, "
                                     f"{size} bytes) — the kernel was told it failed")
                    return False
            except Exception:
                pass
        self._tell(sink, f"⚠ the result of {tool or 'a tool'} could not be delivered to the kernel "
                         f"({last or 'no answer'}) — it is sent again when the kernel asks for it again")
        return False

    @staticmethod
    def _tell(sink, text: str) -> None:
        _note = getattr(sink, "note", None) if sink is not None else None
        if _note is None:
            return
        try:
            _note(text)
        except Exception:
            pass

    async def stop(self) -> None:
        """P5g: server-side stop for Esc/Ctrl-C. Posts a cancel for the
        in-flight turn so the kernel tears down server-side (not just the
        local asyncio task). Fail-soft — the local task.cancel() the dock
        already does is what actually tears the UI down, so a network error
        here must never raise into the key-binding handler. The commands the
        local executor still runs are killed first (review H3): the thread a
        cancelled tool runs in does not stop with the task."""
        _cancel_running(getattr(self, "_executor", None))
        if not self.session_id:
            return
        try:
            import httpx

            headers = await self._headers()
            async with httpx.AsyncClient(base_url=self.cfg.api_url, timeout=10) as client:
                await client.post(
                    f"/v1/agent/sessions/{self.session_id}/cancel", headers=headers,
                )
        except Exception:
            pass
