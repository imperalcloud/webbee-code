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
