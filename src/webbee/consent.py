"""Consent path + the liveness-A consent race (extracted verbatim from
webbee.session): handle_confirm_request relays the user's RAW reply for the
kernel brain to interpret (ICNLI, safe-by-default), and race_consent lets a
pending local prompt RACE the stream so a consent answered from ANOTHER
surface (Telegram relay) unfreezes the terminal instead of leaving the dock
stuck on `approve? y/n`."""
import asyncio
from dataclasses import dataclass

from webbee.frames import _is_foreign_frame, render_foreign_frame


async def handle_confirm_request(frame: dict, mode: str, ask_consent) -> dict:
    """ICNLI consent path. The client does NOT interpret consent words — it
    relays the user's RAW reply; the kernel brain interprets intent
    (safe-by-default). autopilot/plan are explicit and never prompt.
    `ask_consent(app_id, tool, args)` is an async coroutine returning the raw
    reply (resolved through the pinned dock, or a sync fallback reader)."""
    req_id = frame["req_id"]
    if mode == "autopilot":
        return {"req_id": req_id, "result": {"approved": True}}
    if mode == "plan":
        return {"req_id": req_id, "result": {"approved": False, "reason": "plan_mode"}}
    try:
        raw = await ask_consent(frame.get("app_id", ""), frame.get("tool", ""), frame.get("args", {}))
    except Exception:
        # Consent UI failed -> decline (safe-by-default). NEVER leave the kernel
        # hanging on an unanswered confirm — an unposted result froze the dock.
        return {"req_id": req_id, "result": {"approved": False, "reason": "consent_error"}}
    return {"req_id": req_id, "result": {"consent_reply": raw}}


async def _retire(task) -> None:
    """Cancel + drain a racing task so it never outlives the frame loop
    (liveness A hygiene). Absorbs the loser's outcome — CancelledError, a
    late result, StopAsyncIteration, a transport error — retirement must
    never introduce a new failure path. An externally-delivered cancellation
    of the CALLER (task finished before our cancel landed) still propagates."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise                       # ours was the outer cancel — propagate
    except Exception:
        pass


def _dismiss_consent(sink) -> None:
    """Tell the UI the pending consent prompt was answered on ANOTHER surface
    (liveness A). Guarded like the other optional sink hooks — a minimal sink
    without `consent_dismissed` (or a UI bug) must never break the frame loop."""
    fn = getattr(sink, "consent_dismissed", None)
    if fn is None:
        return
    try:
        fn("↩ answered from another surface")
    except Exception:
        pass


@dataclass
class _ConsentRace:
    """Outcome of racing the local consent prompt against the stream
    (liveness A). Exactly one shape per race:
      * out          — the prompt resolved locally (today's path): POST it; a
                       pulled-ahead __anext__ may ride along in carry_task.
      * carry_frame  — the stream won: the park ended elsewhere; the prompt was
                       cancelled + dismissed, nothing is posted, and the pulled
                       frame is handed back to the loop for normal handling.
      * stream_ended — StopAsyncIteration mid-consent: exit as stream end.
    """
    out: dict | None = None
    carry_task: asyncio.Task | None = None
    carry_frame: dict | None = None
    stream_ended: bool = False


async def race_consent(frame: dict, sink, stream, *, mode: str, task_id: str) -> _ConsentRace:
    """LIVE BUG fix (Valentin, 2026-07-15): a consent answered from
    Telegram resolves the kernel park, but the frame loop used to block
    INLINE in handle_confirm_request — the dock froze on `approve? y/n`
    and the rest of the turn never rendered until a local keypress. The
    local prompt now RACES the stream:
      * the prompt resolves first -> POST it (today's path, unchanged);
        the pulled-ahead frame rides back to the loop in carry_task.
      * a re-published confirm_request with the SAME req_id (kernel
        I-MARATHON-USER-WAKE presence re-publish) -> NOT an answer; the
        prompt stays up and both racers keep going.
      * any other OWN-turn frame -> the park ended elsewhere (answered
        from another surface / superseded): cancel the prompt, dismiss the
        dock, POST NOTHING (the kernel accepts only the FIRST result per
        issued req_id), hand the frame back for normal handling.
      * foreign frames (C7) stay display-only and never end the park.
    Consent is never auto-approved here — autopilot/plan resolve instantly
    inside handle_confirm_request exactly as before."""
    rid = frame.get("req_id")
    consent_task = asyncio.ensure_future(
        handle_confirm_request(frame, mode, sink.ask_consent))
    pull = None
    try:
        while True:
            if pull is None:
                pull = asyncio.ensure_future(stream.__anext__())
            await asyncio.wait({consent_task, pull},
                               return_when=asyncio.FIRST_COMPLETED)
            if consent_task.done():
                # The local answer wins — even when both racers finished in
                # the same cycle the pulled frame is NOT lost: the loop owns
                # carry_task and processes it next.
                return _ConsentRace(out=consent_task.result(), carry_task=pull)
            nxt_pull, pull = pull, None
            try:
                nxt = nxt_pull.result()
            except StopAsyncIteration:
                # Stream ended mid-consent: retire the prompt safely and
                # exit exactly as today's stream end (no result posted).
                await _retire(consent_task)
                return _ConsentRace(stream_ended=True)
            if _is_foreign_frame(nxt, task_id):
                render_foreign_frame(nxt, sink)
                continue
            if nxt.get("type") == "confirm_request" and nxt.get("req_id") == rid:
                continue
            await _retire(consent_task)
            _dismiss_consent(sink)
            return _ConsentRace(carry_frame=nxt)
    except BaseException:
        # Hygiene on any unexpected exit (StreamAuthError from the pull,
        # outer cancellation, a consent-task crash): no racer may leak.
        for t in (consent_task, pull):
            if t is not None and not t.done():
                await _retire(t)
        raise
