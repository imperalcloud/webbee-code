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
    # a redraw must not re-read the whole buffer; WITH new output (Task 14), the
    # cache is extended via a positional DELTA read (O(new output)) instead of a
    # full getvalue() re-split — so getvalue() is never called at all here.
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
    pane.console.print("a new line")       # content changed -> incremental delta, not getvalue()
    assert any("a new line" in ln for ln in pane._all_lines())
    assert cio.gv == base, "a print must extend the cache via delta read, not a full getvalue() re-split"


# ── Task 14: incremental line caches + hysteresis trim ──────────────────────
# Even with the getvalue()-memoized-by-position cache above, a changed redraw
# still re-split the WHOLE buffer from scratch (getvalue() + str.split("\n")
# over the entire session) — O(session) per print, quadratic over a long busy
# stream. The cache must extend the cached list IN PLACE from only the delta
# written since the last split.

def test_all_lines_appends_delta_in_place():
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    p.console.print("one")
    first = p._all_lines()
    p.console.print("two")
    second = p._all_lines()
    assert second is first                      # same list object, extended
    assert any("two" in ln for ln in second[-3:])


def test_trim_hysteresis_and_offset_preserved():
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(21000):
        p._io.write(f"line{i}\n")
    p._lines_cache = (None, [""])               # force re-split
    p._offset = 20500                           # reader scrolled up
    p._trim()
    lines = p._all_lines()
    assert len(lines) <= 15001                  # cut to ~15000 (+trailing)
    dropped = 21001 - len(lines)
    assert p._offset == max(0, 20500 - dropped) # view anchored to same content


def test_trim_below_threshold_leaves_buffer_untouched():
    # Hysteresis negative case: below max_lines (20000), _trim must be a
    # complete no-op -- no rewrite of the underlying StringIO at all.
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(18000):
        p._io.write(f"line{i}\n")
    p._lines_cache = (None, [""])               # force re-split
    before = p._io.getvalue()
    p._trim()
    assert p._io.getvalue() == before


def test_trim_keep_floor_not_over_aggressive():
    # A trim that DOES fire must cut down to ~keep (15000), not slash way
    # below it -- pins the keep floor so a future change to the hysteresis
    # constants can't silently turn this into an over-aggressive cut.
    from webbee.output_pane import OutputPane

    p = OutputPane(width=40)
    for i in range(21000):
        p._io.write(f"line{i}\n")
    p._lines_cache = (None, [""])
    p._trim()
    lines = p._all_lines()
    assert len(lines) >= 14000


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


def test_toolbar_shows_reconnecting():
    # Stream transport down mid-turn (W1 front-5): the honest reconnecting
    # state replaces the busy spinner/working line entirely — no fake
    # "working" while the transport is actually down.
    frags = build_toolbar("default", 0, 0, busy=True, reconnecting=3)
    text = _txt(frags)
    assert "⟳ reconnecting (3)" in text and "working" not in text


def test_toolbar_reconnecting_only_applies_while_busy():
    # Idle never shows the reconnecting glyph — busy=False means no turn is
    # running to reconnect for.
    assert "reconnecting" not in _txt(build_toolbar("default", 0, 0, reconnecting=3))


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


def test_drain_pending_requeues_on_start_error_and_survives_mark_error():
    # The popped line is already OUT of `pending` the instant it's read: a
    # `mark` blow-up (pure render-side announcement) must never lose it, and
    # a `start` blow-up must put it BACK at the head before propagating —
    # a broken start must not silently vanish a queued line.
    from webbee.tui import _drain_pending

    def bad_mark(n):
        raise RuntimeError

    def boom(t):
        raise RuntimeError

    q = deque(["a"])
    try:
        _drain_pending(q, boom, mark=bad_mark)
    except RuntimeError:
        pass
    assert list(q) == ["a"]           # nothing lost


def test_drain_pending_mark_error_alone_still_drains():
    from webbee.tui import _drain_pending
    started = []
    q = deque(["a", "b"])

    def bad_mark(n):
        raise RuntimeError

    assert _drain_pending(q, started.append, mark=bad_mark) is True
    assert started == ["a"] and list(q) == ["b"]      # the mark error was swallowed


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


def test_run_session_uses_caller_turn_dict():
    # The repl shares turn_ref with tui.run_session (the poller's lockout-
    # proof gate reads THIS dict) -- when the caller passes turn=, run_session
    # must mutate the SAME object, not a private one of its own.
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
        gate = asyncio.Event()
        busy = {"v": False}
        caller_turn = {"task": None}

        async def on_line(text):
            busy["v"] = True
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
                    turn=caller_turn))
                await asyncio.sleep(0.05)
                pipe.send_text("hello\r")
                await _until(lambda: busy["v"])
                # the SAME dict object the caller holds now carries the live
                # task -- exactly what the repl's poller gate reads.
                assert caller_turn["task"] is not None
                assert not caller_turn["task"].done()
                gate.set()
                await _until(lambda: caller_turn["task"] is None)   # cleared on completion
                pipe.send_text("\x04")                              # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_error_turn_holds_queue():
    # THE other half of the drain rule (W1 task 6): an ERROR-terminated turn
    # must hold the queue exactly like a user stop does -- a broken backend
    # must never burn one queued line per failing turn. on_line completes
    # NORMALLY here (no exception escapes it, no turn["stopped"]) but
    # status()["turn_failed"] is True (repl's except branch calls
    # RichSink.mark_turn_failed(); this fake status mirrors that). Only the
    # NEXT clean completion (turn_failed back to False) drains.
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
        failed = {"v": False}

        async def on_line(text):
            busy["v"] = True
            failed["v"] = False            # begin_turn clears the one-turn flag
            ran.append(text)
            if text == "boom":
                await gate.wait()
                try:
                    raise OSError("network down")
                except OSError:
                    failed["v"] = True     # mark_turn_failed(): swallowed, never crashes the REPL
            busy["v"] = False

        def status():
            return {"tokens": 0, "credits": 0, "busy": busy["v"], "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False,
                    "turn_failed": failed["v"]}

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
                pipe.send_text("boom\r")
                await _until(lambda: ran == ["boom"])          # the ERROR turn is running
                pipe.send_text("queued follow-up\r")           # type-ahead while it runs
                await _until(lambda: list(pend) == ["queued follow-up"])   # queued AT ONCE
                gate.set()                                     # let the error surface + get swallowed
                await _until(lambda: not busy["v"])            # the turn ends NATURALLY (no stop)
                await asyncio.sleep(0.1)
                assert ran == ["boom"]                          # the queue did NOT auto-run…
                assert list(pend) == ["queued follow-up"]      # …preserved, still visible
                assert markers == []                           # …and no drain marker fired
                pipe.send_text("resume\r")                      # a deliberate new (clean) turn
                await _until(lambda: ran == ["boom", "resume", "queued follow-up"])
                assert markers == [0]                           # clean completion DOES drain
                assert not pend
                await _until(lambda: not busy["v"])
                pipe.send_text("\x04")                          # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

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


# ── W1 front-3b task 11: click-to-collapse (header toggles to one row) ──────

def test_queue_collapsed_renders_single_header_row():
    q = deque(["a", "b", "c"])
    frags = queue_fragments(q, collapsed=True, toggle=lambda: None)
    assert len(frags) == 1 and "queued (3)" in frags[0][1] and "▸" in frags[0][1]
    assert len(frags[0]) == 3                      # header carries the toggle handler
    assert queue_height(q, collapsed=True) == 1


def test_queue_header_toggle_fires_on_mouse_up():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    hits = []
    frags = queue_fragments(deque(["a"]), collapsed=False, toggle=lambda: hits.append(1))
    handler = frags[0][2]
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(up) is None and hits == [1]
    scroll = MouseEvent(position=Point(0, 0), event_type=MouseEventType.SCROLL_UP,
                        button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(scroll) is NotImplemented


def test_pull_item_guards_draft_and_stale_index():
    # The ONE pull implementation behind BOTH ↑ and click: never clobbers a
    # typed draft; a stale index (queue drained between render and click) is
    # ignored; a valid pull moves the item out with the cursor at the end.
    from prompt_toolkit.buffer import Buffer
    buf = Buffer(multiline=False)
    buf.text = "half-typed draft"
    pend = deque(["a", "b"])
    assert pull_item(pend, buf, 1) is None                # draft protected
    assert buf.text == "half-typed draft" and list(pend) == ["a", "b"]
    buf.reset()
    assert pull_item(pend, buf, 5) is None                # stale index ignored
    assert pull_item(pend, buf, -1) is None
    assert pull_item(pend, buf, 0) == "a"                 # arbitrary index (click) — the item itself
    assert buf.text == "a" and buf.cursor_position == 1
    assert list(pend) == ["b"]


# ── W1 front-3b task 9: pull-to-edit keeps the original steer_iid ────────────
# A pull used to hand back a bare bool; now it hands back the REMOVED ITEM (or
# None) so the caller can read its carried QueuedLine.iid back out — the whole
# point being an UNCHANGED resubmit keeps the SAME iid (the kernel ring can
# then dedup a landed twin instead of running a genuine duplicate turn).

def test_pull_item_returns_the_item():
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import QueuedLine
    buf = Buffer(multiline=False)
    q = deque([QueuedLine("do x", "iid-1")])
    item = pull_item(q, buf, 0)
    assert item == "do x" and item.iid == "iid-1" and not q


def test_rewrap_pulled_keeps_iid_when_unchanged_and_mints_when_edited():
    from webbee.tui import _rewrap_pulled
    pulled = {"text": "do x", "iid": "iid-1"}
    out = _rewrap_pulled(pulled, "do x")
    assert getattr(out, "iid", "") == "iid-1"
    assert pulled == {"text": "", "iid": ""}          # one-shot: consumed either way
    pulled = {"text": "do x", "iid": "iid-1"}
    out2 = _rewrap_pulled(pulled, "do x HARDER")
    assert getattr(out2, "iid", "") == ""              # edited ⇒ genuinely new message


def test_rewrap_pulled_is_a_noop_when_nothing_was_pulled():
    from webbee.tui import _rewrap_pulled
    pulled = {"text": "", "iid": ""}
    out = _rewrap_pulled(pulled, "typed fresh")
    assert out == "typed fresh" and getattr(out, "iid", "") == ""


def test_pull_then_resubmit_unchanged_keeps_the_original_iid_end_to_end():
    # The actual wiring _enter uses: _arrow_up_action records the pulled
    # item's text + iid into `pulled`; _rewrap_pulled hands the iid back
    # ONLY when the resubmitted text is byte-identical.
    from prompt_toolkit.buffer import Buffer

    from webbee.tui import QueuedLine, _arrow_up_action, _rewrap_pulled
    buf = Buffer(multiline=False)
    pend = deque([QueuedLine("deploy the fix", "iid-42")])
    pulled = {"text": "", "iid": ""}
    _arrow_up_action(_FakeEvent(), buf, {"i": None}, 0, True, pend, pulled)
    assert buf.text == "deploy the fix" and not pend
    resubmitted = _rewrap_pulled(pulled, buf.text)          # unedited resubmit
    assert getattr(resubmitted, "iid", "") == "iid-42"      # SAME iid — kernel ring dedups
    # a second pull, this time edited before resubmit, must NOT carry the iid
    pend.append(QueuedLine("deploy the fix", "iid-42"))
    buf.reset()
    _arrow_up_action(_FakeEvent(), buf, {"i": None}, 0, True, pend, pulled)
    edited = _rewrap_pulled(pulled, buf.text + " NOW")
    assert getattr(edited, "iid", "") == ""                 # edited ⇒ no carried iid


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


# ── 0.3.14: cross-surface (remote-queued) rows in the live panel ─────────────
# Full-queue-layer K1: a Telegram/panel follow-up queued into the RUNNING
# kernel session shows in the SAME live panel the instant the kernel announces
# it (task_queued) — rendered ABOVE the local rows (the kernel drains its own
# queue first, mid-run), tagged [origin], and DISPLAY-ONLY: never a mouse
# handler, never part of the ↑/click pull index space (you can't pull a
# kernel-queued item into the local input buffer).

def _remote(origin, text, iid=""):
    return {"origin": origin, "text": text, "iid": iid}


def test_remote_rows_render_above_local_tagged_by_origin():
    frags = queue_fragments(deque(["local line"]),
                            remote=[_remote("telegram", "fix the tests", "i1"),
                                    _remote("web-panel", "then the docs", "i2")])
    text = _panel_text(frags)
    assert "⋯ queued (3)" in text                       # header counts BOTH queues
    assert "[telegram] fix the tests" in text
    assert "[web-panel] then the docs" in text
    # remote rows sit ABOVE the local ones — top→bottom stays drain order
    assert text.index("[telegram]") < text.index("[web-panel]") < text.index("local line")
    assert not NO_CYRILLIC.search(text)


def test_remote_rows_are_display_only_never_pullable():
    pulls = []
    frags = queue_fragments(deque(["mine"]), pull=pulls.append, width=80,
                            remote=[_remote("telegram", "remote thing", "i1")])
    # every remote row is a plain 2-tuple (no mouse handler); local rows keep
    # their handler and their LOCAL index (remote rows never shift pull math)
    remote_frags = [f for f in frags if "[telegram]" in f[1]]
    assert remote_frags and all(len(f) == 2 for f in remote_frags)
    handlers = [f[2] for f in frags if len(f) == 3]
    assert len(handlers) == 1                           # only the local row is clickable
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    handlers[0](up)
    assert pulls == [0]                                 # pending[0] == "mine", not the remote row


def test_remote_only_queue_renders_panel_without_pull_hint():
    frags = queue_fragments(deque(), remote=[_remote("telegram", "go", "i1")])
    text = _panel_text(frags)
    assert "⋯ queued (1)" in text and "[telegram] go" in text
    assert "↑ edit last" not in text                    # nothing local to pull/edit
    assert all(len(f) == 2 for f in frags)              # nothing clickable at all


def test_remote_rows_styled_distinct_and_one_line_truncated():
    frags = queue_fragments(deque(), width=40,
                            remote=[_remote("telegram", "x" * 500, "i1")])
    row = [f for f in frags if "[telegram]" in f[1]][0]
    assert row[0] == "class:qp.remote"
    line = row[1].lstrip("\n")
    assert line.endswith("…") and len(line) <= 40       # one panel row, hard-capped


def test_remote_rows_cap_with_a_more_row_and_height_counts_both():
    rem = [_remote("telegram", f"r{i}", f"i{i}") for i in range(7)]
    frags = queue_fragments(deque(["a"]), remote=rem)
    text = _panel_text(frags)
    assert "… +2 more" in text                          # oldest 2 remote hide
    assert "[telegram] r6" in text and "[telegram] r1" not in text
    assert queue_height(deque(["a"]), rem) == 1 + (QP_MAX_ITEMS + 1) + 1  # hdr + remote(5+more) + local
    assert queue_height(deque(), [_remote("telegram", "go")]) == 2        # hdr + 1 remote
    assert queue_height(deque(), []) == 0               # both empty → hidden
    assert queue_height(deque(["a"]))  == 2             # remote omitted → exactly as before


def test_remote_parked_row_renders_with_pause_prefix():
    # W1 front-3b: a row that survived a marathon PARK is tagged parked=True
    # by RichSink.end_turn -- the panel must show it's still queued
    # server-side (not phantom) with a ⏸ prefix, distinct from a live row.
    parked = _remote("telegram", "do x", "i1")
    parked["parked"] = True
    frags = queue_fragments(deque(), remote=[parked, _remote("web-panel", "live one", "i2")])
    text = _panel_text(frags)
    assert "⏸ [telegram] do x" in text
    assert "[web-panel] live one" in text and "⏸ [web-panel]" not in text


# ── 0.3.15: mid-turn inject — Enter-while-busy FLIES into the RUNNING turn ────
# The type-ahead used to hold every busy-typed line client-side until turn end
# (_drain_pending), so a running marathon never saw it mid-turn. Now the enter
# handler routes the busy path through an inject launcher: _inject_or_queue
# mints the steer_iid, POSTs immediately (repl._inject_via_gateway → gateway
# /inject → kernel task_id-less new_task → K2 fly-in at the next brain step)
# and falls back to today's local queue — carrying the SAME iid so the kernel
# dedup ring drops the twin — only when the inject fails. Idle Enter and the
# no-launcher path (fallback loop) are byte-identical to before.

def test_enter_while_busy_with_inject_wired_flies_not_queues():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started, injected = [], []
    res = _submit_line("deploy the fix", buf, pending, True, started.append,
                       inject=injected.append)
    assert res == "injected"
    assert injected == ["deploy the fix"]            # flew to the launcher NOW
    assert not pending and started == []             # never held locally, never a new turn
    assert buf.history.items == ["deploy the fix"]   # ↑-recall unchanged


def test_idle_enter_with_inject_wired_still_starts_normally():
    from webbee.tui import _submit_line
    buf = _RecBuf()
    pending = deque()
    started, injected = [], []
    res = _submit_line("hello", buf, pending, False, started.append,
                       inject=injected.append)
    assert res == "started" and started == ["hello"]
    assert injected == [] and not pending            # idle path byte-identical


def test_inject_ok_is_kernel_owned_nothing_queued_locally():
    from webbee.tui import _inject_or_queue

    async def scenario():
        pending = deque()
        calls = []

        async def inject(text, iid):
            calls.append((text, iid))
            return True

        ok = await _inject_or_queue(inject, "fly this in", pending)
        assert ok is True
        assert not pending                            # kernel-owned — no local row
        (text, iid), = calls
        assert text == "fly this in"
        assert re.fullmatch(r"[0-9a-f]{32}", iid)     # uuid4 hex, minted at enqueue
    asyncio.run(scenario())


def test_inject_or_queue_reuses_carried_iid():
    # A QueuedLine (a pull-to-edit resubmitted unchanged, see _rewrap_pulled)
    # already carries the original steer_iid -- _inject_or_queue must fly it
    # under THAT id, not mint a fresh one, so the kernel ring can still dedup
    # a landed twin.
    from webbee.tui import QueuedLine, _inject_or_queue

    async def scenario():
        pending = deque()
        calls = []

        async def inject(text, iid):
            calls.append((text, iid))
            return True

        ok = await _inject_or_queue(inject, QueuedLine("t", "iid-9"), pending)
        assert ok is True
        (text, iid), = calls
        assert text == "t" and iid == "iid-9"         # carried, not a fresh uuid
    asyncio.run(scenario())


def test_inject_failure_falls_back_to_local_queue_with_same_iid():
    from webbee.tui import QueuedLine, _inject_or_queue

    async def scenario():
        for failing in (
            lambda text, iid: _false(),               # gateway said no / no session
            lambda text, iid: _boom(),                # network error raised
        ):
            pending = deque()
            seen = {}

            async def inject(text, iid, f=failing):
                seen["iid"] = iid
                return await f()

            ok = await _inject_or_queue(inject, "queued instead", pending)
            assert ok is False
            assert list(pending) == ["queued instead"]   # today's local fallback
            item = pending[0]
            assert isinstance(item, QueuedLine)
            # the SAME iid rides the fallback row → the turn-end drain re-submits
            # under it and the kernel ring dedups if the inject landed after all
            assert item.iid == seen["iid"]

    async def _false():
        return False

    async def _boom():
        raise RuntimeError("offline")

    asyncio.run(scenario())


def test_queued_line_is_a_plain_str_everywhere_it_flows():
    from webbee.tui import QueuedLine
    from webbee.queue_panel import one_line
    q = QueuedLine("fix the  tests", "i1")
    assert q == "fix the  tests" and isinstance(q, str)
    assert q.iid == "i1"
    assert one_line(q, 80) == "fix the tests"         # panel row unchanged
    assert getattr("plain", "iid", "") == ""          # a typed line has none


def test_dock_end_to_end_busy_enter_injects_ok_and_failure_falls_back():
    # Drive the REAL Application: while turn 1 runs, the first busy Enter
    # flies through the inject leg (nothing queued locally, no drain later);
    # the second busy Enter hits a failing inject and falls back to the local
    # queue, draining at turn end through the SAME on_line path — its
    # QueuedLine still carrying the minted iid.
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
        ran, injected = [], []
        pending = deque()
        gate = asyncio.Event()
        busy = {"v": False}

        async def on_line(text):
            busy["v"] = True
            ran.append(text)
            await gate.wait()
            busy["v"] = False

        async def inject(text, iid):
            injected.append((text, iid))
            return text == "flies in"                 # the second line fails

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
                    pending=pending, inject=inject))
                await asyncio.sleep(0.05)
                pipe.send_text("first\r")
                await _until(lambda: ran == ["first"])          # turn 1 running
                pipe.send_text("flies in\r")                     # busy → inject ok
                await _until(lambda: len(injected) == 1)
                assert injected[0][0] == "flies in"
                assert re.fullmatch(r"[0-9a-f]{32}", injected[0][1])
                assert not pending                               # kernel-owned
                pipe.send_text("falls back\r")                   # busy → inject FAILS
                await _until(lambda: list(pending) == ["falls back"])
                fallback_iid = pending[0].iid
                assert injected[1][1] == fallback_iid            # same iid, both legs
                assert ran == ["first"]                          # nothing ran mid-turn
                gate.set()                                       # finish the turns
                await _until(lambda: ran == ["first", "falls back"])
                assert getattr(ran[1], "iid", "") == fallback_iid  # iid rode the drain
                await _until(lambda: not busy["v"] and not pending)
                pipe.send_text("\x04")                           # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


def test_dock_mounts_sticky_todo_panel_above_queue_panel():
    # The layout contract: root HSplit = [pane, TODO panel, QUEUE panel,
    # input, toolbar]. Both are ConditionalContainers occupying ZERO rows when
    # empty (pixel-identical empty dock); the todo panel shows the moment the
    # sink-owned list fills, updates height in place, and STAYS visible while
    # the queue panel independently tracks the pending deque.
    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import ConditionalContainer
    from prompt_toolkit.output import DummyOutput

    from webbee import tui

    async def scenario():
        pane = tui.OutputPane(width=80)
        todos, pending = [], deque()

        def status():
            return {"tokens": 0, "credits": 0, "busy": False, "current": "",
                    "elapsed": 0.0, "tools": 0, "consent": False}

        async def on_line(text): ...

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    pane=pane, on_line=on_line, mode_getter=lambda: "default",
                    on_cycle=lambda: None, status=status, is_busy=lambda: False,
                    consent_pending=lambda: False, resolve_consent=lambda t: None,
                    pending=pending, todos=todos))
                await asyncio.sleep(0.05)
                conds = [c for c in get_app().layout.container.children
                         if isinstance(c, ConditionalContainer)]
                assert len(conds) == 2
                todo_cc, queue_cc = conds                # todo mounts ABOVE queue
                assert not todo_cc.filter() and not queue_cc.filter()   # empty dock
                todos.append({"content": "fix the bug", "status": "in_progress"})
                assert todo_cc.filter() and not queue_cc.filter()       # todo only
                assert todo_cc.content.height() == 2     # header + the ▶ row
                todos.append({"content": "run tests", "status": "pending"})
                assert todo_cc.content.height() == 3     # updates in place
                pending.append("queued line")
                assert queue_cc.filter()                 # queue joins independently
                todos.clear()
                assert not todo_cc.filter()              # /clear empties → hidden
                pipe.send_text("\x04")                   # Ctrl-D exit (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())
