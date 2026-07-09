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


# ── inline renderer: no alternate screen, no mouse capture ────────────────────
# The renderer is a PromptSession under patch_stdout — finalized output goes to
# the terminal's NATIVE scrollback, so the terminal owns selection/copy/scroll.
# There must be NO full-screen Application anywhere in the module.

def test_no_full_screen_application_in_tui_source():
    import inspect

    from webbee import tui
    src = inspect.getsource(tui)
    assert "full_screen" not in src        # never take the alternate screen
    assert "Application(" not in src       # never build a full-screen Application
    assert "mouse_support=True" not in src  # never capture the mouse


def test_richsink_prints_to_real_stdout(capsys):
    # RichSink's default Console writes to the REAL stdout (not a StringIO pane),
    # so under patch_stdout it commits into the native scrollback.
    from webbee.render import RichSink
    s = RichSink()
    s.note("hello inline")
    out = capsys.readouterr().out
    assert "hello inline" in out


# ── Enter-dispatch decision logic (pure) — mirrors the old dock Enter binding ──

def _decide(text, *, consent=False, busy=False, sel_i=None, steps=True):
    from webbee.tui import _decide_enter
    return _decide_enter(text, consent_pending=lambda: consent,
                         is_busy=lambda: busy, sel={"i": sel_i},
                         has_steps_nav=steps)


def test_decide_enter_consent_pending_relays_reply():
    assert _decide("yes please", consent=True) == ("consent", "yes please")


def test_decide_enter_consent_wins_even_when_busy():
    # A consent reply can arrive mid-turn (busy) — consent takes priority.
    assert _decide("n", consent=True, busy=True) == ("consent", "n")


def test_decide_enter_empty_with_selection_expands_step():
    assert _decide("  ", sel_i=2) == ("expand", 2)


def test_decide_enter_empty_selection_ignored_when_busy():
    # Busy blocks step-expand (the selection nav is idle-only).
    assert _decide("", sel_i=2, busy=True) == ("ignore", None)


def test_decide_enter_busy_ignores_text():
    assert _decide("do a thing", busy=True) == ("ignore", None)


def test_decide_enter_empty_ignored():
    assert _decide("   ", sel_i=None) == ("ignore", None)


def test_decide_enter_text_starts_turn():
    assert _decide("fix the bug") == ("turn", "fix the bug")


def test_decide_enter_expand_needs_steps_nav():
    # Empty line + a selection but NO steps_nav wiring → not an expand.
    assert _decide("", sel_i=1, steps=False) == ("ignore", None)


# ── inline run_session builds a PromptSession, not a full-screen Application ───

def test_run_session_builds_inline_promptsession(monkeypatch):
    import contextlib

    import prompt_toolkit
    import prompt_toolkit.patch_stdout as _ps
    from webbee import tui

    captured = {}

    class _FakePromptSession:
        def __init__(self, **kw):
            captured.update(kw)

        async def prompt_async(self, *a, **k):
            raise EOFError            # break the loop immediately → clean exit

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _FakePromptSession)
    monkeypatch.setattr(_ps, "patch_stdout", lambda *a, **k: contextlib.nullcontext())

    status = {"busy": False, "current": "", "elapsed": 0.0, "tools": 0,
              "tokens": 0, "credits": 0, "consent": False}

    async def _noop_line(_):
        return None

    ok = asyncio.run(tui.run_session(
        on_line=_noop_line, mode_getter=lambda: "default", on_cycle=lambda: None,
        status=lambda: status, is_busy=lambda: False,
        consent_pending=lambda: False, resolve_consent=lambda r: None))

    assert ok is True
    assert captured["mouse_support"] is False          # never hijack native selection
    assert captured["refresh_interval"] == 0           # ticker invalidates, not auto-repaint
    assert callable(captured["bottom_toolbar"])        # status line is a callable
    assert "auto_suggest" in captured and "history" in captured


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
