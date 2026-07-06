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
    assert "51.0k" in t
    assert "66 credits" in t
    assert "Shift + TAB" in t          # spelled in words, no glyph
    assert "⇧⇥" not in t     # the ⇧⇥ glyph must NOT appear
    assert not NO_CYRILLIC.search(t)


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


def test_escape_while_busy_schedules_server_stop_and_leaves_selection():
    from webbee.tui import _escape_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    sel = {"i": 2}
    _escape_action(sel, lambda: True, stop_turn, event)

    assert len(event.app.background_tasks) == 1
    _run_and_drain(event.app.background_tasks[0])
    assert stop_calls == [1]
    # Esc-while-busy interrupts — it does NOT also clear the step-selection
    # or invalidate the toolbar (that's the idle-path job, tested below).
    assert sel["i"] == 2
    assert event.app.invalidated is False


def test_escape_while_idle_clears_step_selection_unchanged():
    from webbee.tui import _escape_action

    stop_calls = []

    async def stop_turn():
        stop_calls.append(1)

    event = _FakeEvent()
    sel = {"i": 2}
    _escape_action(sel, lambda: False, stop_turn, event)

    assert sel["i"] is None
    assert event.app.invalidated is True
    assert event.app.background_tasks == []
    assert stop_calls == []


def test_escape_while_busy_with_no_stop_turn_falls_back_to_clearing():
    # stop_turn=None (e.g. no repl wiring) must never crash Esc — it just
    # keeps the pre-P5g behavior of clearing the step-selection.
    from webbee.tui import _escape_action

    event = _FakeEvent()
    sel = {"i": 1}
    _escape_action(sel, lambda: True, None, event)

    assert sel["i"] is None
    assert event.app.invalidated is True
    assert event.app.background_tasks == []


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
