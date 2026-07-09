"""Inline REPL renderer (like Claude Code's classic renderer, but sturdier).

`run_session` drives a `prompt_toolkit.PromptSession` loop wrapped in
`patch_stdout()`: finalized output (assistant replies, tool results, progress,
streaming tokens) is printed to the REAL stdout by the Rich Console and
patch_stdout commits it ABOVE the prompt into the terminal's NATIVE scrollback.
There is NO alternate screen and NO mouse capture — the terminal itself owns
selection / copy / scroll / find / tmux. A persistent `bottom_toolbar` shows
status + an animated spinner (ticked by a manual 0.25s invalidate loop, since
`refresh_interval` doesn't repaint toolbar content — prompt_toolkit #751).

Turns run as background asyncio tasks so the prompt keeps accepting input while
output streams above it — this is also what lets a consent reply arrive as the
next accepted line. Pure helpers (next_mode/build_toolbar/_decide_enter) and the
key-binding actions (_escape_action/_interrupt_action) are unit-tested without
spinning up a real Application. Grounded in prompt_toolkit 3.0.52."""
import asyncio

from webbee.render import _fmt_tokens

_MODES = ("default", "plan", "autopilot")
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille frames — animated while a turn runs


def next_mode(mode: str) -> str:
    try:
        return _MODES[(_MODES.index(mode) + 1) % len(_MODES)]
    except ValueError:
        return _MODES[0]


def build_toolbar(mode: str, tokens: int, credits: int, *, busy: bool = False,
                  current: str = "", elapsed: float = 0.0, tools: int = 0,
                  consent: bool = False) -> list:
    """The status line under the prompt, as prompt_toolkit formatted text
    (per-segment styled). Three states: consent (awaiting a reply), busy (a turn
    is running — an ANIMATED coloured spinner + the current action in accent, so
    it pops, not grey), and idle (mode value coloured PER MODE — default cyan /
    plan purple / autopilot yellow — + SESSION spend + the Shift + TAB hint).
    Style classes are defined in run_session's Style."""
    if consent:
        return [("class:tb.consent", "  approve? type y / n / a reply · Enter to send")]
    if busy:
        spin = _SPINNER[int(elapsed * 10) % len(_SPINNER)]   # animates via the ticker
        frags = [("class:tb.spin", f"  {spin} "), ("class:tb.working", "working")]
        if current:
            frags += [("class:tb.dim", " · "), ("class:tb.action", current)]
        frags.append(("class:tb.dim",
                      f" · {elapsed:.0f}s · {tools} · {_fmt_tokens(tokens)} tok"
                      f"   ·   Esc/Ctrl-C to stop"))
        return frags
    return [("class:tb.dim", "  mode: "),
            (f"class:tb.mode.{mode}", mode),
            ("class:tb.dim",
             f"   ·   {_fmt_tokens(tokens)} tok · {credits} credits   ·   Shift + TAB: switch mode")]


def _escape_action(sel: dict, is_busy, stop_turn, event) -> None:
    """Esc key binding (P5g). While a turn is running, ask the server to stop
    it (`stop_turn` posts the cancel; fail-soft, best-effort) and leave the
    step-selection untouched — while idle (unchanged), clear the selection."""
    if is_busy() and stop_turn is not None:
        event.app.create_background_task(stop_turn())
        return
    sel["i"] = None
    event.app.invalidate()


def _interrupt_action(turn: dict, is_busy, stop_turn, event) -> None:
    """Ctrl-C key binding (P5g). The LOCAL task.cancel() is what actually tears
    the running turn down; ALSO ask the server to stop the turn so it doesn't
    keep running (and spending) after the client moves on."""
    t = turn["task"]
    if t is not None and not t.done():
        if is_busy() and stop_turn is not None:
            event.app.create_background_task(stop_turn())
        t.cancel()                          # cancel the running turn; the loop survives


def _decide_enter(text: str, *, consent_pending, is_busy, sel: dict,
                  has_steps_nav: bool):
    """PURE decision for one accepted input line — mirrors the old dock's Enter
    binding EXACTLY. Returns (action, payload):
      ("consent", text)  a consent reply is pending → relay the raw reply
      ("expand", idx)    empty line + a step is selected + idle → drill into it
      ("ignore", None)   busy, or an empty line with nothing selected → drop it
      ("turn", text)     otherwise → start a turn with this text
    Consent takes priority over everything (it can arrive mid-turn)."""
    if consent_pending():
        return ("consent", text)
    if not text.strip() and sel.get("i") is not None and has_steps_nav and not is_busy():
        return ("expand", sel["i"])
    if is_busy() or not text.strip():
        return ("ignore", None)
    return ("turn", text)


async def run_session(*, on_line, mode_getter, on_cycle, status, is_busy,
                      consent_pending, resolve_consent, steps_nav=None,
                      stop_turn=None) -> bool:
    """The inline REPL: a PromptSession loop under patch_stdout. Each accepted
    line either resolves a pending consent (ICNLI: raw verbatim), expands a
    selected step, or starts a turn as a BACKGROUND task (the loop keeps
    prompting so output streams above + the next consent reply can be typed).
    When `steps_nav` is given and the input is empty and no turn is running,
    Up/Down move a step selection (toolbar shows `step k/N`) and Enter expands
    it via `steps_nav["expand"]`; Esc clears it. Returns True on clean exit;
    False if prompt_toolkit is unavailable (caller uses the plain fallback)."""
    try:
        import os

        from prompt_toolkit import PromptSession
        from prompt_toolkit.application import get_app_or_none
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style
    except Exception:
        return False

    turn = {"task": None}
    sel = {"i": None}   # None = no selection; else 0-based step index

    async def _run_turn(text):
        try:
            await on_line(text)
        finally:
            turn["task"] = None
            app = get_app_or_none()
            if app is not None:
                app.invalidate()

    def _nav_count() -> int:
        try:
            return int(steps_nav["count"]()) if steps_nav else 0
        except Exception:
            return 0

    kb = KeyBindings()

    @kb.add("s-tab")
    def _cycle(event):
        on_cycle()
        event.app.invalidate()

    @kb.add("c-c")
    def _interrupt(event):
        _interrupt_action(turn, is_busy, stop_turn, event)

    @kb.add("c-d")
    def _eof(event):
        # Exit only when idle — never abandon a running turn.
        if not is_busy():
            event.app.exit(exception=EOFError)

    @kb.add("escape", eager=True)
    def _esc(event):
        _escape_action(sel, is_busy, stop_turn, event)

    @Condition
    def _steps_nav_active() -> bool:
        # Up/Down drive step-navigation ONLY on an empty, idle prompt with steps
        # to show; otherwise they fall through to their default (history) role.
        app = get_app_or_none()
        if app is None:
            return False
        return (not app.current_buffer.text) and (not is_busy()) and _nav_count() > 0

    @kb.add("up", filter=_steps_nav_active)
    def _step_up(event):
        n = _nav_count()
        sel["i"] = (n - 1) if sel["i"] is None else max(0, sel["i"] - 1)
        event.app.invalidate()

    @kb.add("down", filter=_steps_nav_active)
    def _step_down(event):
        n = _nav_count()
        sel["i"] = 0 if sel["i"] is None else min(n - 1, sel["i"] + 1)
        event.app.invalidate()

    def _toolbar():
        if sel["i"] is not None and steps_nav:
            return [("class:tb.dim",
                     f"  step {sel['i'] + 1}/{_nav_count()} · Enter to expand · Esc to cancel")]
        st = status()
        return build_toolbar(mode_getter(), st["tokens"], st["credits"], busy=st["busy"],
                             current=st["current"], elapsed=st["elapsed"],
                             tools=st["tools"], consent=st["consent"])

    style = Style.from_dict({
        "bottom-toolbar": "noreverse",       # a calm status line, not the default reverse bar
        "prompt": "#00afd7 bold",            # cyan ❯ — the interactive accent
        "tb.dim": "#8a8a8a",                 # idle chrome / secondary bits — dim
        "tb.spin": "#e8a317 bold",           # animated spinner — bee-yellow, pops
        "tb.working": "#e8a317",             # 'working' — yellow
        "tb.action": "#00afd7",              # current action — cyan
        "tb.consent": "#e8a317 bold",        # consent prompt line — yellow
        "tb.mode.default": "#00afd7",        # default — cyan
        "tb.mode.plan": "#af87ff",           # plan — purple
        "tb.mode.autopilot": "#e8a317 bold", # autopilot — yellow (auto-approving: caution)
    })

    # Persistent history + suggestions — UX wins the full-screen dock lacked.
    history = None
    try:
        d = os.path.expanduser("~/.cache/webbee")
        os.makedirs(d, exist_ok=True)
        history = FileHistory(os.path.join(d, "history"))
    except Exception:
        history = None

    session = PromptSession(
        history=history,
        auto_suggest=AutoSuggestFromHistory(),
        key_bindings=kb,
        bottom_toolbar=_toolbar,
        style=style,
        mouse_support=False,     # never hijack the terminal's native selection
        refresh_interval=0,      # no auto-repaint — the ticker below invalidates
        erase_when_done=True,    # the typed line is re-committed by sink.user_echo
    )

    async def _ticker():
        # Animate the spinner + tick the elapsed clock while a turn runs.
        while True:
            await asyncio.sleep(0.25)
            if is_busy():
                app = get_app_or_none()
                if app is not None:
                    app.invalidate()

    message = [("class:prompt", "❯ ")]
    tick = asyncio.ensure_future(_ticker())
    try:
        with patch_stdout(raw=True):
            while True:
                try:
                    text = await session.prompt_async(message)
                except (EOFError, KeyboardInterrupt):
                    break
                action, payload = _decide_enter(
                    text, consent_pending=consent_pending, is_busy=is_busy,
                    sel=sel, has_steps_nav=steps_nav is not None)
                if action == "consent":
                    resolve_consent(payload)              # ICNLI: relay the raw reply verbatim
                elif action == "expand":
                    sel["i"] = None
                    # ensure_future (not the prompt app's background task): a new
                    # PromptSession app is created per iteration, so a task tied
                    # to the app would be torn down the instant we loop.
                    asyncio.ensure_future(steps_nav["expand"](payload))
                elif action == "turn":
                    turn["task"] = asyncio.ensure_future(_run_turn(payload))
                # "ignore" → just loop and prompt again
    finally:
        tick.cancel()
    return True
