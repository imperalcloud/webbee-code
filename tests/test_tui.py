import asyncio
import re

from webbee.tui import next_mode, build_toolbar

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def _txt(frags):
    """Join prompt_toolkit formatted-text fragments into the visible string."""
    return "".join(seg for _, seg in frags)


def test_next_mode_cycles():
    assert next_mode("default") == "plan"
    assert next_mode("plan") == "autopilot"
    assert next_mode("autopilot") == "default"

def test_next_mode_unknown_resets():
    assert next_mode("weird") == "default"

def test_toolbar_idle_has_mode_tokens_cost_and_hint():
    t = _txt(build_toolbar("plan", 51000, 66))
    assert "plan" in t
    assert "51k" in t
    assert "66 credits" in t
    assert "Shift + TAB" in t          # spelled in words, no glyph
    assert "⇧⇥" not in t     # the ⇧⇥ glyph must NOT appear
    assert not NO_CYRILLIC.search(t)


def test_toolbar_humanizes_millions_and_credits():
    # Big numbers stay readable: tokens + credits both scale to k/M/B.
    t = _txt(build_toolbar("default", 1_500_000, 2_000_000))
    assert "1.5M tok" in t
    assert "2M credits" in t


def test_fmt_tokens_scales_k_m_b():
    from webbee.render import _fmt_tokens
    assert _fmt_tokens(900) == "900"
    assert _fmt_tokens(2100) == "2.1k"
    assert _fmt_tokens(51000) == "51k"
    assert _fmt_tokens(1_500_000) == "1.5M"
    assert _fmt_tokens(2_000_000) == "2M"
    assert _fmt_tokens(3_200_000_000) == "3.2B"


def test_toolbar_busy_state_shows_working_dot_and_stop_hint():
    t = _txt(build_toolbar("default", 1200, 0.0143, busy=True,
                           current="notes·delete_note", elapsed=4, tools=3))
    assert "working" in t and "notes·delete_note" in t
    assert "Esc/Ctrl-C to stop" in t and "4s" in t
    assert not NO_CYRILLIC.search(t)


def test_toolbar_consent_state():
    t = _txt(build_toolbar("default", 0, 0.0, consent=True))
    assert "approve?" in t and "Enter to send" in t
    assert not NO_CYRILLIC.search(t)


def test_toolbar_busy_spinner_animates_with_elapsed():
    a = _txt(build_toolbar("default", 0, 0.0, busy=True, elapsed=0.0))
    b = _txt(build_toolbar("default", 0, 0.0, busy=True, elapsed=0.4))
    assert a != b                       # the spinner frame advances as time passes
    assert "working" in a and "working" in b


def test_toolbar_mode_colored_per_mode():
    # each mode carries its own style class so the eye catches the mode at a glance
    def _mode_style(mode):
        for style, seg in build_toolbar(mode, 0, 0.0):
            if seg == mode:
                return style
        return ""
    assert _mode_style("autopilot") == "class:tb.mode.autopilot"
    assert _mode_style("plan") == "class:tb.mode.plan"
    assert _mode_style("default") == "class:tb.mode.default"


def test_output_pane_captures_colored_text():
    # The pane's Rich Console renders into a buffer as ANSI (colours preserved
    # for the FormattedTextControl to show); construction works headlessly.
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.console.print("[bold red]Error[/] ok")
    out = pane.dump()
    assert "Error" in out and "ok" in out
    assert "\x1b[" in out          # ANSI colour escapes preserved for the pane


# ── copy-on-select (drag → OSC 52) ────────────────────────────────────────────

def test_selected_text_single_line():
    from webbee.tui import OutputPane
    from prompt_toolkit.data_structures import Point
    pane = OutputPane(width=80)
    pane.console.print("hello world")
    assert pane._selected_text(Point(6, 0), Point(10, 0)) == "world"


def test_selected_text_multi_line_strips_ansi():
    from webbee.tui import OutputPane
    from prompt_toolkit.data_structures import Point
    pane = OutputPane(width=80)
    pane.console.print("abcdef")
    pane.console.print("[bold]ghijkl[/]")   # coloured — must be stripped
    pane.console.print("mnopqr")
    assert pane._selected_text(Point(3, 0), Point(2, 2)) == "def\nghijkl\nmno"


def test_selected_text_reversed_order_normalizes():
    from webbee.tui import OutputPane
    from prompt_toolkit.data_structures import Point
    pane = OutputPane(width=80)
    pane.console.print("hello")
    assert pane._selected_text(Point(4, 0), Point(0, 0)) == "hello"


def test_copy_flash_expires():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane.copy_flash = "✓ copied 5 chars"
    pane._flash_until = 0.0            # already in the past
    assert pane.flash() == ""


# ── virtualization: render only the visible slice, follow the tail ────────────

def test_pane_follows_tail_on_notify():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(50)))   # 50 lines
    pane.notify()
    assert pane._offset == len(pane._all_lines()) - 10          # pinned to the bottom


def test_pane_scroll_up_pauses_follow_then_rearms():
    from webbee.tui import OutputPane
    pane = OutputPane(width=80)
    pane._view_h = 10
    pane._io.write("\n".join(f"line{i}" for i in range(50)))
    pane.notify()                      # offset=40, following
    pane.scroll(-5)
    assert pane._offset == 35 and pane._follow is False
    pane.scroll(100)                   # clamp back to bottom
    assert pane._offset == 40 and pane._follow is True


def test_selected_text_respects_scroll_offset():
    from webbee.tui import OutputPane
    from prompt_toolkit.data_structures import Point
    pane = OutputPane(width=80)
    pane._io.write("aaa\nbbb\nccc\nddd\n")
    pane._offset = 2                   # viewport top = content line 2 ("ccc")
    assert pane._selected_text(Point(0, 0), Point(2, 0)) == "ccc"


def test_output_pane_no_full_reread_on_unchanged_redraw():
    # Perf regression (long-session lag): _all_lines re-read the ENTIRE buffer
    # (getvalue O(n) + full string compare O(n)) on EVERY redraw. Every keystroke
    # / ticker tick / scroll in a big session cost O(session). With NO new output,
    # a redraw must not re-read the whole buffer.
    import io

    from webbee.output_pane import OutputPane

    class CountingIO(io.StringIO):
        def __init__(self):
            super().__init__()
            self.gv = 0

        def getvalue(self):
            self.gv += 1
            return super().getvalue()

    pane = OutputPane(width=80)
    cio = CountingIO()
    pane._io = cio
    pane.console.file = cio
    pane.console.print("some output line")
    pane._all_lines()                      # warm the cache
    base = cio.gv
    pane._all_lines(); pane._all_lines(); pane._all_lines()   # no new writes since
    assert cio.gv == base, "re-read the whole buffer on unchanged redraws (O(session)/frame)"
    pane.console.print("a new line")       # content changed -> one refresh is expected
    assert any("a new line" in ln for ln in pane._all_lines())
    assert cio.gv > base


# ── P5g: Esc/Ctrl-C stop the SERVER turn, not just the local task ────────────
# Previously Ctrl-C only cancelled the local asyncio task while the cloud
# brain kept running server-side; Esc did nothing while busy. Both key
# bindings are wired through `_escape_action`/`_interrupt_action` (pure,
# dependency-injected — same testing philosophy as next_mode/build_toolbar)
# so this is exercised without spinning up a real prompt_toolkit Application.

class _FakeApp:
    def __init__(self):
        self.background_tasks = []
        self.invalidated = False

    def create_background_task(self, coro):
        self.background_tasks.append(coro)

    def invalidate(self):
        self.invalidated = True


class _FakeEvent:
    def __init__(self):
        self.app = _FakeApp()


def _run_and_drain(coro):
    """Actually execute a coroutine handed to create_background_task so it
    doesn't leak an 'never awaited' warning, and to prove the spy fired."""
    asyncio.run(coro)


class _FakeTask:
    def __init__(self): self.cancelled = []
    def done(self): return False
    def cancel(self): self.cancelled.append(1)


def test_escape_while_busy_cancels_turn_and_schedules_server_stop():
    # Esc now REALLY stops a running turn: cancel the local turn task (what tears
    # it down) AND ask the server to stop — matching the "Esc/Ctrl-C" hint.
    from webbee.tui import _escape_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    sel = {"i": 2}
    task = _FakeTask()
    turn = {"task": task}
    _escape_action(sel, turn, lambda: True, stop_turn, event)

    assert task.cancelled == [1]                  # the local turn is actually cancelled
    assert len(event.app.background_tasks) == 1
    _run_and_drain(event.app.background_tasks[0])
    assert stop_calls == [1]                       # AND the server is asked to stop
    # Busy-path stops the turn — it does NOT also clear the step-selection.
    assert sel["i"] == 2
    assert event.app.invalidated is False


def test_escape_while_idle_clears_step_selection_unchanged():
    from webbee.tui import _escape_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    sel = {"i": 2}
    turn = {"task": None}
    _escape_action(sel, turn, lambda: False, stop_turn, event)

    assert sel["i"] is None
    assert event.app.invalidated is True
    assert event.app.background_tasks == []
    assert stop_calls == []


def test_escape_while_busy_with_no_stop_turn_still_cancels_locally():
    # stop_turn=None (e.g. no repl wiring) must never crash Esc — it still cancels
    # the local turn task, so Esc reliably stops even without the server hook.
    from webbee.tui import _escape_action

    event = _FakeEvent()
    sel = {"i": 1}
    task = _FakeTask()
    turn = {"task": task}
    _escape_action(sel, turn, lambda: True, None, event)

    assert task.cancelled == [1]                  # local teardown happens regardless
    assert event.app.background_tasks == []        # no stop_turn -> nothing scheduled
    assert sel["i"] == 1                            # busy-path leaves the selection


def test_interrupt_while_busy_cancels_local_task_and_schedules_server_stop():
    from webbee.tui import _interrupt_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    cancelled = []

    class _FakeTask:
        def done(self):
            return False

        def cancel(self):
            cancelled.append(1)

    event = _FakeEvent()
    turn = {"task": _FakeTask()}
    _interrupt_action(turn, lambda: True, stop_turn, event)

    assert cancelled == [1]                      # local teardown still happens
    assert len(event.app.background_tasks) == 1  # AND the server gets asked to stop
    _run_and_drain(event.app.background_tasks[0])
    assert stop_calls == [1]


def test_interrupt_with_no_running_task_is_a_noop():
    from webbee.tui import _interrupt_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    turn = {"task": None}
    _interrupt_action(turn, lambda: True, stop_turn, event)

    assert event.app.background_tasks == []
    assert stop_calls == []


# ---- mouse-report flood hardening (0.3.3) ----------------------------------
# Linux (occasionally macOS) terminals under prompt_toolkit's default
# mouse_support=True run ANY-EVENT tracking (?1003): every bare mouse move
# fires an SGR report. Under that flood the vt100 parser splits sequences at
# read-chunk boundaries — the ESC arrives as a phantom Escape KEY (which used
# to stop the running turn!) and the tail ("35;6;42M…") lands in the input
# buffer as literal text.

_GARBAGE = "roo35;6;42M<35;35;46M5;49;46M3M35;71;37M29M5;94;19M5;101;14M4M"


def test_scrub_mouse_residue_removes_leaked_reports():
    from webbee.tui import scrub_mouse_residue
    out = scrub_mouse_residue(_GARBAGE)
    assert "42M" not in out and "<35" not in out
    assert out.startswith("roo")            # non-residue text survives


def test_scrub_mouse_residue_keeps_normal_text():
    from webbee.tui import scrub_mouse_residue
    for s in ("fix the tests", "a;b;c", "35;6", "надо дочинить endpoint'ы", ""):
        assert scrub_mouse_residue(s) == s


class _FakeOutput:
    def __init__(self):
        self.raw = []

    def write_raw(self, s):
        self.raw.append(s)


def test_configure_mouse_modes_button_event_not_any_event():
    from webbee.tui import configure_mouse_modes
    out = _FakeOutput()
    configure_mouse_modes(out)
    out.enable_mouse_support()
    joined = "".join(out.raw)
    assert "\x1b[?1002h" in joined           # drag/wheel/click tracking
    assert "\x1b[?1003h" not in joined       # NEVER any-event (bare-move flood)
    out.raw.clear()
    out.disable_mouse_support()
    joined = "".join(out.raw)
    assert "\x1b[?1002l" in joined and "\x1b[?1003l" in joined  # belt & braces


def test_configure_mouse_modes_skips_outputs_without_write_raw():
    from webbee.tui import configure_mouse_modes
    class _Plain:
        pass
    p = _Plain()
    configure_mouse_modes(p)                 # must not raise, must not add attrs
    assert not hasattr(p, "enable_mouse_support")


class _FakeBuf:
    def __init__(self, text=""):
        self.text = text


def test_phantom_escape_with_residue_cleans_buffer_not_the_turn():
    # A mouse-report flood in the input buffer means this Escape is almost
    # certainly a SPLIT SEQUENCE, not the user — never kill the turn on it.
    from webbee.tui import _escape_action

    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    buf = _FakeBuf("draft " + _GARBAGE)
    _escape_action({"i": None}, turn, lambda: True, None, event, buf=buf)
    assert task.cancelled == []              # the turn SURVIVES
    assert "42M" not in buf.text             # the residue is cleaned
    assert buf.text.startswith("draft roo")


def test_real_escape_with_clean_buffer_still_stops_the_turn():
    from webbee.tui import _escape_action

    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    _escape_action({"i": None}, turn, lambda: True, None, event, buf=_FakeBuf("draft reply"))
    assert task.cancelled == [1]             # normal Esc behavior unchanged


# ── Unit Q (0.3.11): type-ahead queue + up-arrow history recall ───────────────
# Enter while a turn runs used to ERASE the typed line (buf.reset() before the
# busy gate). Now it queues (Claude-Code type-ahead): the line runs after the
# current turn, the toolbar shows the depth, and up-arrow recalls submitted
# lines to edit/resend. Handlers delegate to module-level *_action helpers —
# same DI testing philosophy as _escape_action/_interrupt_action above.

from collections import deque   # noqa: E402


class _RecBuf:
    """Buffer stand-in for _submit_line: records history appends."""
    def __init__(self, text=""):
        self.text = text
        class _H:
            def __init__(self): self.items = []
            def append_string(self, s): self.items.append(s)
        self.history = _H()


def test_enter_while_busy_queues_line_not_erased():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    assert _submit_line("deploy the fix", buf, pending, True, started.append) == "queued"
    assert list(pending) == ["deploy the fix"]      # preserved, NOT erased
    assert started == []                             # and NOT run mid-turn
    assert buf.history.items == ["deploy the fix"]   # recallable via up-arrow


def test_submit_while_idle_starts_turn_immediately():
    # Idle + non-empty = today's normal submit, byte-identical behavior.
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    assert _submit_line("hello", buf, pending, False, started.append) == "started"
    assert started == ["hello"] and not pending
    assert buf.history.items == ["hello"]            # typed turns are recallable too


def test_toolbar_shows_queue_depth_busy_and_idle():
    busy = _txt(build_toolbar("default", 0, 0, busy=True, elapsed=1.0, queued=2))
    assert "⋯2 queued" in busy and "working" in busy
    idle = _txt(build_toolbar("default", 0, 0, queued=1))
    assert "⋯1 queued" in idle and "Shift + TAB" in idle


def test_toolbar_hides_queue_segment_when_empty():
    assert "queued" not in _txt(build_toolbar("default", 0, 0, busy=True, elapsed=1.0))
    assert "queued" not in _txt(build_toolbar("default", 0, 0))


def test_turn_completion_drains_oldest_queued_to_submit_path():
    from webbee.tui import _drain_pending
    started = []
    pending = deque(["first", "second"])
    assert _drain_pending(pending, started.append) is True
    assert started == ["first"]                      # OLDEST goes out…
    assert list(pending) == ["second"]               # …one per completion, rest stay


def test_drain_with_empty_queue_is_a_noop():
    from webbee.tui import _drain_pending
    started = []
    assert _drain_pending(deque(), started.append) is False
    assert started == []


def test_fifo_order_across_multiple_queued():
    from webbee.tui import _drain_pending, _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    for t in ("a", "b", "c"):
        _submit_line(t, buf, pending, True, started.append)
    assert started == []                             # nothing runs mid-turn
    while _drain_pending(pending, started.append):   # successive turn completions
        pass
    assert started == ["a", "b", "c"]                # strict FIFO


def test_up_arrow_recalls_last_submitted_line():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action, _submit_line

    async def scenario():
        buf = Buffer(multiline=False)
        _submit_line("fix the flaky test", buf, deque(), True, lambda t: None)
        buf.load_history_if_not_yet_loaded()   # BufferControl does this each repaint
        await buf._load_history_task           # loader fills the working lines
        event = _FakeEvent()
        sel = {"i": None}
        _arrow_up_action(event, buf, sel, 0, True)   # busy → history, never step-nav
        assert buf.text == "fix the flaky test"
        assert sel["i"] is None                       # step selection untouched
    asyncio.run(scenario())


def test_down_arrow_cycles_history_forward():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_down_action, _arrow_up_action

    async def scenario():
        buf = Buffer(multiline=False)
        buf.history.append_string("older")
        buf.history.append_string("newer")
        buf.load_history_if_not_yet_loaded()
        await buf._load_history_task
        event = _FakeEvent()
        _arrow_up_action(event, buf, {"i": None}, 0, True)
        _arrow_up_action(event, buf, {"i": None}, 0, True)
        assert buf.text == "older"
        _arrow_down_action(event, buf, {"i": None}, 0, True)
        assert buf.text == "newer"
    asyncio.run(scenario())


def test_up_arrow_with_text_present_pulls_history_not_steps():
    # The step-nav gate requires EMPTY input — with a draft in the box the
    # arrow edits history even when idle with steps present.
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action

    async def scenario():
        buf = Buffer(multiline=False)
        buf.history.append_string("submitted earlier")
        buf.text = "draf"
        buf.load_history_if_not_yet_loaded()
        await buf._load_history_task
        event = _FakeEvent()
        sel = {"i": None}
        _arrow_up_action(event, buf, sel, 3, False)   # idle + steps, but text present
        assert buf.text == "submitted earlier"
        assert sel["i"] is None                        # no step selection created
    asyncio.run(scenario())


def test_up_arrow_during_step_nav_still_navigates_steps():
    # Idle + empty input + steps present = EXACTLY today's step-navigation.
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_down_action, _arrow_up_action

    buf = Buffer(multiline=False)                     # empty input, no history touch
    sel = {"i": None}
    event = _FakeEvent()
    _arrow_up_action(event, buf, sel, 3, False)
    assert sel["i"] == 2 and buf.text == ""           # selection moves, no history pull
    _arrow_up_action(event, buf, sel, 3, False)
    assert sel["i"] == 1
    _arrow_down_action(event, buf, sel, 3, False)
    assert sel["i"] == 2
    assert event.app.invalidated is True


def test_dock_end_to_end_type_ahead_queues_then_drains_fifo():
    # Drive the REAL Application (pipe input + dummy output): Enter while a
    # turn runs queues the lines instead of erasing them, the drain in
    # _run_turn's finally submits them one per turn-completion, FIFO, through
    # the SAME on_line path a typed line takes; Ctrl-D exits when idle.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran = []
        gate = asyncio.Event()
        busy = {"v": False}

        async def on_line(text):
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    pane=pane, on_line=on_line, mode_getter=lambda: "default",
                    on_cycle=lambda: None, status=status,
                    is_busy=lambda: busy["v"],
                    consent_pending=lambda: False, resolve_consent=lambda t: None))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])      # turn 1 is running
                pipe.send_text("second\r")                   # typed WHILE busy → queue
                pipe.send_text("third\r")
                await asyncio.sleep(0.1)
                assert ran == ["first"]                      # nothing runs mid-turn
                gate.set()                                   # let the turns finish
                await _until(lambda: ran == ["first", "second", "third"])   # FIFO
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                       # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── 0.3.12: the queue is VISIBLE + managed (0.3.13: visibility moved from the
# static `⋯ queued:` scrollback echo — which scrolled away, duplicated and went
# stale — into the LIVE queue panel above the input, webbee.queue_panel; the
# panel section below covers it). A drain still announces itself
# (sink.queued_run) right before the drained line's normal ❯ user-echo, the
# toolbar segment is an ACCENT (tb.working, not dim), /queue // /queue clear
# manage the shared deque even mid-turn, and a user STOP (Esc/Ctrl-C)
# PRESERVES the queue — drain on natural completion only.


def test_whitespace_never_queues():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started = []
    for junk in ("", "   ", "\t \t"):
        assert _submit_line(junk, buf, pending, True, started.append) == "ignored"
        assert _submit_line(junk, buf, pending, False, started.append) == "ignored"
    assert not pending and started == []
    assert buf.history.items == []                   # junk is not recallable either


def test_drain_marks_the_start_with_remaining_depth():
    from webbee.tui import _drain_pending
    started, marks = [], []
    pending = deque(["a", "b"])
    assert _drain_pending(pending, started.append, mark=marks.append) is True
    assert started == ["a"] and marks == [1]         # announced with what's left waiting
    assert _drain_pending(pending, started.append, mark=marks.append) is True
    assert started == ["a", "b"] and marks == [1, 0]


def test_toolbar_queued_segment_is_accent_not_dim():
    for frags in (build_toolbar("default", 0, 0, busy=True, elapsed=1.0, queued=2),
                  build_toolbar("default", 0, 0, queued=2)):
        styles = [style for style, seg in frags if "queued" in seg]
        assert styles == ["class:tb.working"]        # pops in busy AND idle, never dim


def test_is_queue_command_matches_queue_and_subcommands_only():
    from webbee.tui import _is_queue_command
    assert _is_queue_command("/queue")
    assert _is_queue_command("  /QUEUE clear ")
    assert not _is_queue_command("/queues")
    assert not _is_queue_command("queue")
    assert not _is_queue_command("/status")
    assert not _is_queue_command("")


def test_escape_stop_flags_the_turn_so_the_queue_is_preserved():
    from webbee.tui import _escape_action
    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    _escape_action({"i": None}, turn, lambda: True, None, event, buf=_FakeBuf(""))
    assert task.cancelled == [1] and turn.get("stopped") is True


def test_ctrl_c_stop_flags_the_turn_so_the_queue_is_preserved():
    from webbee.tui import _interrupt_action
    event = _FakeEvent()
    task = _FakeTask()
    turn = {"task": task}
    _interrupt_action(turn, lambda: True, None, event)
    assert task.cancelled == [1] and turn.get("stopped") is True


def test_dock_stop_preserves_queue_then_natural_completion_drains():
    # THE rock-solid rule, end-to-end on the real Application: a user STOP
    # (Ctrl-C — deterministic in a pipe, no Esc flush timeout) never auto-runs
    # the queue; the queued line survives, and only the NEXT natural turn
    # completion drains it (with the queued_run marker).
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran, markers = [], []
        gate = asyncio.Event()
        busy = {"v": False}

        async def on_line(text):
            busy["v"] = True
            ran.append(text)
            try:
                await gate.wait()
            except asyncio.CancelledError:
                pass                    # repl._run_turn absorbs a user stop
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        pend = deque()
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    pane=pane, on_line=on_line, mode_getter=lambda: "default",
                    on_cycle=lambda: None, status=status,
                    is_busy=lambda: busy["v"],
                    consent_pending=lambda: False, resolve_consent=lambda t: None,
                    pending=pend, queued_run=markers.append))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])         # turn 1 is running
                pipe.send_text("queued follow-up\r")           # type-ahead while busy
                await _until(lambda: list(pend) == ["queued follow-up"])   # queued AT ONCE
                pipe.send_text("\x03")                         # user STOP (Ctrl-C)
                await _until(lambda: not busy["v"])
                await asyncio.sleep(0.1)
                assert ran == ["first"]                        # the queue did NOT auto-run
                assert list(pend) == ["queued follow-up"]      # preserved, still visible
                assert markers == []                           # and no drain marker fired
                gate.set()                                     # next turns complete at once
                pipe.send_text("resume\r")                     # a deliberate new turn
                await _until(lambda: ran == ["first", "resume", "queued follow-up"])
                assert markers == [0]                          # the drain announced itself
                assert not pend
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                         # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True
        assert "⋯ queued" not in pane.dump()      # the transcript stays CLEAN

    asyncio.run(scenario())


def test_dock_queue_command_runs_immediately_even_while_busy():
    # /queue must manage the queue exactly when it matters — MID-TURN. It goes
    # straight to on_line as a background task (never type-ahead-queued), so
    # /queue clear typed while busy empties the shared deque before the turn
    # ends, and nothing drains at completion.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran = []
        gate = asyncio.Event()
        busy = {"v": False}
        pend = deque()

        async def on_line(text):
            if text.split()[0].lower() == "/queue":      # the repl handler: display-only
                ran.append(text)
                if "clear" in text:
                    pend.clear()
                return
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    pane=pane, on_line=on_line, mode_getter=lambda: "default",
                    on_cycle=lambda: None, status=status,
                    is_busy=lambda: busy["v"],
                    consent_pending=lambda: False, resolve_consent=lambda t: None,
                    pending=pend))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])
                pipe.send_text("second\r")                     # queued (type-ahead)
                await _until(lambda: list(pend) == ["second"])
                pipe.send_text("/queue\r")                     # runs NOW, while busy
                await _until(lambda: "/queue" in ran)
                assert list(pend) == ["second"]                # listing didn't touch it
                pipe.send_text("/queue clear\r")               # drops it, still mid-turn
                await _until(lambda: not pend)
                gate.set()                                     # turn 1 finishes naturally
                await _until(lambda: not busy["v"])
                await asyncio.sleep(0.1)
                assert "second" not in ran                     # cleared → nothing drained
                pipe.send_text("\x04")
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# ── 0.3.13: the LIVE queue panel + pull-to-edit ───────────────────────────────
# The static `⋯ queued:` scrollback echoes are GONE (they scrolled away,
# duplicated, and went stale when edited). The queue now lives in a panel
# pinned ABOVE the input (webbee.queue_panel — pure fragment/height builders
# mounted in a ConditionalContainer): it updates live on every add/edit/drain,
# ↑ on an empty input pulls the NEWEST item back into the box for editing
# (re-submit re-queues it), a click pulls THAT item, and the transcript stays
# real-turns-only.

from webbee.queue_panel import (  # noqa: E402
    QP_MAX_ITEMS, one_line, pull_item, queue_fragments, queue_height)


def _panel_text(frags):
    return "".join(f[1] for f in frags)


def test_empty_queue_renders_no_panel():
    assert queue_fragments(deque()) == []
    assert queue_height(deque()) == 0        # ConditionalContainer hides it anyway


def test_panel_header_counts_and_orders_items_drain_first_to_newest():
    frags = queue_fragments(deque(["first", "second"]))
    text = _panel_text(frags)
    assert "⋯ queued (2)" in text
    assert text.index("first") < text.index("second")   # top row drains next (FIFO)
    assert text.count("\n") == 2                         # header + exactly one row each
    assert not NO_CYRILLIC.search(text)


def test_panel_newest_item_accented_older_muted():
    frags = queue_fragments(deque(["older", "newest"]))
    assert [f[0] for f in frags if "older" in f[1]] == ["class:qp.item"]
    assert [f[0] for f in frags if "newest" in f[1]] == ["class:qp.last"]


def test_panel_caps_at_newest_five_with_a_more_row():
    items = [f"item{i}" for i in range(8)]
    frags = queue_fragments(deque(items))
    text = _panel_text(frags)
    assert "⋯ queued (8)" in text                        # header keeps the TRUE depth
    assert "… +3 more" in text                           # the oldest 3 hide behind it
    assert all(t in text for t in items[3:])             # newest 5 shown
    assert all(t not in text for t in items[:3])


def test_panel_height_is_header_plus_rows_capped():
    assert queue_height(deque(["a"])) == 2                       # header + 1
    assert queue_height(deque(["a"] * QP_MAX_ITEMS)) == 6        # header + 5
    assert queue_height(deque(["a"] * 9)) == 7                   # header + 5 + more-row


def test_panel_item_is_one_truncated_row():
    frags = queue_fragments(deque(["x" * 500]), width=40)
    rows = _panel_text(frags).split("\n")
    assert len(rows) == 2                                # a huge item still costs ONE row
    assert rows[1].endswith("…") and len(rows[1]) <= 40


def test_panel_multiline_item_collapses_to_one_row():
    frags = queue_fragments(deque(["line1\nline2\tline3"]))
    text = _panel_text(frags)
    assert text.count("\n") == 1 and "line1 line2 line3" in text


def test_one_line_collapses_whitespace_and_truncates():
    assert one_line("a\n  b\t c", 80) == "a b c"
    assert one_line("abcdef", 4) == "abc…"
    assert one_line("abcd", 4) == "abcd"
    assert one_line("", 10) == ""
    assert one_line("abcdef", 0) == "abcdef"             # no width → no truncation


def test_panel_without_pull_has_no_mouse_handlers():
    assert all(len(f) == 2 for f in queue_fragments(deque(["a", "b"])))


def test_panel_item_mouse_up_pulls_that_item_other_events_fall_through():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    pulls = []
    frags = queue_fragments(deque(["a", "b", "c"]), pull=pulls.append, width=80)
    handlers = [f[2] for f in frags if len(f) == 3]
    assert len(handlers) == 3                            # every item row is clickable
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handlers[1](up) is None and pulls == [1]      # the CLICKED item, by index
    down = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_DOWN,
                      button=MouseButton.LEFT, modifiers=frozenset())
    assert handlers[0](down) is NotImplemented           # press/drag/wheel fall through
    assert pulls == [1]


def test_control_dispatches_click_row_to_the_matching_item():
    # prompt_toolkit's OWN per-fragment dispatch (FormattedTextControl.mouse_handler)
    # routes a click at row y to that row's handler — the exact path a real SGR
    # ?1000 button report takes after decoding. Row 0 = header (no handler).
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    pulls = []
    pend = deque(["alpha", "beta"])
    ctrl = FormattedTextControl(
        lambda: queue_fragments(pend, pull=pulls.append, width=80), focusable=False)
    ctrl.create_content(width=80, height=3)              # populates self._fragments
    ev = lambda y: MouseEvent(position=Point(4, y), event_type=MouseEventType.MOUSE_UP,
                              button=MouseButton.LEFT, modifiers=frozenset())
    assert ctrl.mouse_handler(ev(2)) is None and pulls == [1]    # row 2 = "beta"
    assert ctrl.mouse_handler(ev(1)) is None and pulls == [1, 0] # row 1 = "alpha"
    assert ctrl.mouse_handler(ev(0)) is NotImplemented           # header: no pull


def test_pull_item_guards_draft_and_stale_index():
    # The ONE pull implementation behind BOTH ↑ and click: never clobbers a
    # typed draft; a stale index (queue drained between render and click) is
    # ignored; a valid pull moves the item out with the cursor at the end.
    from prompt_toolkit.buffer import Buffer
    buf = Buffer(multiline=False)
    buf.text = "half-typed draft"
    pend = deque(["a", "b"])
    assert pull_item(pend, buf, 1) is False              # draft protected
    assert buf.text == "half-typed draft" and list(pend) == ["a", "b"]
    buf.reset()
    assert pull_item(pend, buf, 5) is False              # stale index ignored
    assert pull_item(pend, buf, -1) is False
    assert pull_item(pend, buf, 0) is True               # arbitrary index (click)
    assert buf.text == "a" and buf.cursor_position == 1
    assert list(pend) == ["b"]


def test_up_arrow_pulls_newest_queued_into_empty_buffer_busy_or_idle():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action
    for busy in (True, False):        # NOT busy-gated: the queue survives Esc into idle
        buf = Buffer(multiline=False)
        pend = deque(["older", "newest"])
        event = _FakeEvent()
        sel = {"i": None}
        _arrow_up_action(event, buf, sel, 0, busy, pend)
        assert buf.text == "newest"                      # the last thing queued
        assert buf.cursor_position == len("newest")      # cursor ready at the end
        assert list(pend) == ["older"]                   # it LEFT the queue
        assert sel["i"] is None and event.app.invalidated is True


def test_repeated_pulls_walk_newest_to_oldest():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action
    buf = Buffer(multiline=False)
    pend = deque(["a", "b", "c"])
    event = _FakeEvent()
    for expected in ("c", "b", "a"):
        buf.reset()                                      # emptied (submitted/cleared)…
        _arrow_up_action(event, buf, {"i": None}, 0, True, pend)
        assert buf.text == expected                      # …then the next-newest pulls
    assert not pend


def test_up_arrow_with_text_present_never_clobbers_the_draft():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action

    async def scenario():
        buf = Buffer(multiline=False)
        buf.history.append_string("submitted earlier")
        buf.text = "half-typed draft"
        buf.load_history_if_not_yet_loaded()
        await buf._load_history_task
        pend = deque(["queued line"])
        _arrow_up_action(_FakeEvent(), buf, {"i": None}, 0, True, pend)
        assert list(pend) == ["queued line"]             # NOT pulled — buffer has text
        assert buf.text == "submitted earlier"           # history served instead
    asyncio.run(scenario())


def test_pull_queued_takes_precedence_over_step_nav():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import _arrow_up_action
    buf = Buffer(multiline=False)
    pend = deque(["queued line"])
    sel = {"i": None}
    _arrow_up_action(_FakeEvent(), buf, sel, 3, False, pend)   # idle + steps + queue
    assert buf.text == "queued line" and not pend        # the queued text is fresher intent
    assert sel["i"] is None                              # steps reachable once queue empties


def test_dock_end_to_end_panel_pull_reedit_requeue_and_clean_transcript():
    # The full loop on the REAL Application: queue while busy → the item is in
    # the PANEL (queue_fragments over the shared deque), NOT in the scrollback;
    # ↑ pulls it into the input (it leaves the panel); Enter re-queues it at
    # the tail; the natural drain announces itself (queued_run) and empties the
    # panel; the transcript never saw a `⋯ queued` echo.
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def _until(pred, timeout=5.0):
        t0 = time.time()
        while not pred():
            assert time.time() - t0 < timeout, "timed out"
            await asyncio.sleep(0.01)

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran, markers = [], []
        gate = asyncio.Event()
        busy = {"v": False}
        pend = deque()

        async def on_line(text):
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    pane=pane, on_line=on_line, mode_getter=lambda: "default",
                    on_cycle=lambda: None, status=status,
                    is_busy=lambda: busy["v"],
                    consent_pending=lambda: False, resolve_consent=lambda t: None,
                    pending=pend, queued_run=markers.append))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])           # turn 1 is running
                pipe.send_text("second draft\r")                 # type-ahead → queue
                await _until(lambda: list(pend) == ["second draft"])
                assert "second draft" in _panel_text(queue_fragments(pend))  # in the PANEL
                assert "⋯ queued" not in pane.dump()             # NOT in the scrollback
                pipe.send_text("\x1b[A")                         # ↑ pulls it to edit
                await _until(lambda: not pend)                   # it LEFT the panel
                pipe.send_text(" v2\r")                          # edit + re-submit (busy)
                await _until(lambda: list(pend) == ["second draft v2"])   # re-queued at tail
                assert ran == ["first"]                          # still nothing mid-turn
                gate.set()                                       # natural completion
                await _until(lambda: ran == ["first", "second draft v2"])
                assert markers == [0]                            # drain announced itself
                await _until(lambda: not pend)                   # panel emptied
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                           # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True
        assert "⋯ queued" not in pane.dump()                     # transcript stayed CLEAN

    asyncio.run(scenario())
