"""0.3.36 terminal UX pass — the five things reported live by Valentin:

  1. a newline INSIDE the prompt, on a chord that works on every device;
  2. Home's bottom-right version badge, with a freshness check;
  3. session tokens + credits visible at the bottom of EVERY tab;
  4. Home showing the session's TOTAL credits;
  5. `/login` (and the other account commands) working ON Home.

Every test here is pure/offline: no network, no real terminal, no Application.
"""
import json

from webbee.commands import CommandContext, dispatch
from webbee.home_view import (HomeActions, HomeData, HomeView, TabRow,
                              build_home_model, session_totals, version_badge)
from webbee.repl import (_HOME_GATED_ACTIONS, _SaySink, _format_sessions_plain,
                         _slot_ctx)
from webbee.slots import SessionSlot, SlotManager
from webbee.tui import build_toolbar, enable_csi_u_newline, input_rows
from webbee.update import check_for_update, check_update_status


# --------------------------------------------------------------------------
# 1. newline in the prompt
# --------------------------------------------------------------------------
def test_csi_u_shift_enter_parses_as_alt_enter():
    """Shift+Enter (CSI-u `ESC [13;2u`) must arrive as the SAME (escape, c-m)
    pair Alt+Enter produces, so ONE binding serves both chords."""
    from prompt_toolkit.input import vt100_parser as vp
    assert enable_csi_u_newline() is True
    keys = []
    p = vp.Vt100Parser(lambda kp: keys.append(kp))
    p.feed("\x1b[13;2u")
    p.flush()
    assert [k.key.value for k in keys] == ["escape", "c-m"]


def test_csi_u_registration_is_idempotent_and_additive():
    """Re-running it never overwrites an existing sequence (no key regresses)."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys
    before_cr = ANSI_SEQUENCES.get("\r")
    enable_csi_u_newline()
    enable_csi_u_newline()
    assert ANSI_SEQUENCES.get("\r") is before_cr        # plain Enter untouched
    assert ANSI_SEQUENCES["\x1b[13;2u"] == (Keys.Escape, Keys.ControlM)


def test_ctrl_j_is_not_the_same_key_as_enter():
    """The universal fallback must be a DIFFERENT key from Enter, otherwise
    binding it would break submitting."""
    from prompt_toolkit.keys import Keys
    assert Keys.ControlJ is not Keys.ControlM
    assert Keys.ControlJ.value == "c-j" and Keys.ControlM.value == "c-m"


def test_buffer_holds_embedded_newlines_and_box_grows():
    """A multi-line draft survives in the (multiline=False) submit-on-Enter
    buffer, and the input box's height math counts the extra rows."""
    from prompt_toolkit.buffer import Buffer
    b = Buffer(multiline=False)
    b.insert_text("first")
    b.insert_text("\n")
    b.insert_text("second")
    assert b.text == "first\nsecond"
    assert input_rows(b.text, 40, 10) == 2
    assert input_rows("a" * 45 + "\n" + "b", 40, 10) == 3   # wrap + explicit LF


# --------------------------------------------------------------------------
# 2. version badge + freshness
# --------------------------------------------------------------------------
def test_version_badge_update_available():
    txt, style = version_badge(
        "0.3.35", "🐝 webbee v0.3.36 available — upgrade: pipx upgrade webbee")
    assert txt == "v0.3.35 → 0.3.36 available"
    assert style == "class:home.update"


def test_version_badge_up_to_date_only_when_actually_checked():
    assert version_badge("0.3.36", "", checked=True) == ("v0.3.36 · up to date",
                                                         "class:home.fresh")
    # offline / not yet checked: NO freshness claim at all
    assert version_badge("0.3.36", "", checked=False) == ("v0.3.36",
                                                          "class:home.dim")


def test_version_badge_never_raises_on_junk():
    for args in ((None, None), ("", ""), ("x.y", "webbee vNOPE available")):
        txt, style = version_badge(*args)
        assert isinstance(txt, str) and style.startswith("class:")


def test_check_update_status_reports_checked_flag(tmp_path):
    cache = tmp_path / "u.json"
    notice, checked = check_update_status("0.1.0", cache_path=cache,
                                          now=1000.0, fetch=lambda: "0.2.0")
    assert "0.2.0" in notice and checked is True

    notice, checked = check_update_status("0.2.0", cache_path=tmp_path / "b.json",
                                         now=1000.0, fetch=lambda: "0.2.0")
    assert notice == "" and checked is True          # checked, nothing newer

    notice, checked = check_update_status("0.2.0", cache_path=tmp_path / "c.json",
                                         now=1000.0, fetch=lambda: None)
    assert notice == "" and checked is False         # offline -> no claim


def test_check_update_status_uses_cache_and_keeps_legacy_helper(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text(json.dumps({"latest": "9.9.9", "checked_at": 1000.0}))

    def _boom():
        raise AssertionError("must not fetch inside the TTL")

    notice, checked = check_update_status("0.1.0", cache_path=cache, now=1001.0,
                                         fetch=_boom)
    assert "9.9.9" in notice and checked is True
    # the pre-0.3.36 entry point still behaves exactly as before
    assert "9.9.9" in check_for_update("0.1.0", cache_path=cache, now=1001.0,
                                       fetch=_boom)


def test_home_footer_puts_the_badge_bottom_right():
    view = _view(HomeData(version="0.3.35",
                          update_notice="🐝 webbee v0.3.36 available",
                          update_checked=True), _slots())
    lines = [_text(ln) for ln in view._all_lines()]
    last = lines[-1]
    assert last.rstrip().endswith("v0.3.35 → 0.3.36 available")
    assert last.startswith(" ")                       # right-aligned, padded
    assert "Alt+↵ newline" in "\n".join(lines)         # the chord is discoverable


# --------------------------------------------------------------------------
# 3 + 4. spend visible on every tab, totalled on Home
# --------------------------------------------------------------------------
class _Sink:
    def __init__(self, tokens, credits):
        self._t, self._c = tokens, credits

    def status(self):
        return {"tokens": self._t, "credits": self._c}

    def consent_pending(self):
        return False

    def is_busy(self):
        return False


def _slots(*pairs):
    mgr = SlotManager()
    mgr.add(SessionSlot(kind="home", workspace="/w", label="Home",
                        pane=object(), sink=None, agent=None))
    for i, (tk, cr) in enumerate(pairs, 1):
        mgr.add(SessionSlot(kind="session", workspace=f"/w/{i}", label=f"t{i}",
                            pane=object(), sink=_Sink(tk, cr), agent=None))
    mgr.active_idx = 0
    return mgr


def test_session_totals_sums_every_tab_and_skips_home():
    assert session_totals(_slots((1200, 34), (800, 16))) == (2000, 50)
    assert session_totals(_slots()) == (0, 0)          # Home only


def test_session_totals_survives_a_broken_sink():
    mgr = _slots((100, 5))

    class _Bad:
        def status(self):
            raise RuntimeError("nope")

    mgr.add(SessionSlot(kind="session", workspace="/w/x", label="bad",
                        pane=object(), sink=_Bad(), agent=None))
    assert session_totals(mgr) == (100, 5)             # bad tab contributes 0


def test_home_toolbar_shows_the_session_total():
    """Home has no sink, so its toolbar must show the ADDED-UP spend."""
    tk, cr = session_totals(_slots((128000, 340), (45000, 120)))
    assert (tk, cr) == (173000, 460)
    text = "".join(seg for _, seg in build_toolbar("default", tk, cr))
    assert "173k tok" in text and "460 credits" in text


def test_every_session_tab_toolbar_shows_tokens_and_credits():
    for busy in (False, True):
        text = "".join(seg for _, seg in
                       build_toolbar("plan", 2100, 7, busy=busy, current="x"))
        assert "2.1k" in text                          # tokens, compact
    assert "7 credits" in "".join(seg for _, seg in
                                  build_toolbar("plan", 2100, 7))


def _text(line):
    return "".join(t[1] for t in line)


def _view(data, slots):
    noop = lambda *a, **k: None
    actions = HomeActions(new_session=noop, open_recent=noop, switch_tab=noop,
                          close_tab=noop, set_tab_mode=noop, set_notify=noop,
                          set_new_tab_mode=noop, top_up=noop,
                          open_security_docs=noop, sign_in=noop)
    return HomeView(slots=slots, actions=actions, data=data, width=100)


def test_home_dashboard_shows_total_credits_spent_this_session():
    view = _view(HomeData(version="0.3.36"), _slots((128000, 340), (45000, 120)))
    body = "\n".join(_text(ln) for ln in view._all_lines())
    assert "spent this session" in body
    assert "460 credits" in body and "173k tokens" in body


# --------------------------------------------------------------------------
# 5. account commands work on Home
# --------------------------------------------------------------------------
def test_account_commands_are_no_longer_home_gated():
    for action in ("login", "logout", "sessions", "sessions_revoke",
                   "logout_others", "cost"):
        assert action not in _HOME_GATED_ACTIONS, action


def test_genuinely_session_scoped_commands_stay_gated():
    for action in ("steps", "checkpoints", "rollback", "notify", "mode",
                   "queue", "rename"):
        assert action in _HOME_GATED_ACTIONS, action


def test_login_dispatches_to_the_login_action():
    ctx = CommandContext(mode="default", workspace="/w", version="0.3.36",
                         surface="terminal", logged_in=False,
                         session_tokens=0, session_credits=0, git_branch="")
    res = dispatch("/login", ctx)
    assert res.handled and res.action == "login"


def test_say_sink_routes_login_output_into_homes_pane():
    """`login_device_flow` only ever calls .note()/.login_prompt() on the sink;
    on Home there is none, so the shim must carry those to the pane."""
    printed = []

    class _Console:
        width = 80

        def print(self, *a, **k):
            printed.append(" ".join(str(x) for x in a))

    class _Pane:
        console = _Console()

    slot = SessionSlot(kind="home", workspace="/w", label="Home",
                       pane=_Pane(), sink=None, agent=None)
    sink = _SaySink(slot)
    sink.note("hello from login")
    sink.login_prompt("ABCD-1234", "https://panel.imperal.io/device")
    body = "\n".join(printed)
    assert "hello from login" in body
    assert "ABCD-1234" in body and "device" in body


def test_slot_ctx_on_home_reports_the_session_total():
    mgr = _slots((1000, 25), (500, 5))
    home = mgr.slots[0]
    ctx = _slot_ctx(home, logged_in=True, slots=mgr)
    assert (ctx.session_tokens, ctx.session_credits) == (1500, 30)
    # /cost then answers with the real figure instead of Home's zeros
    assert "1500 tokens" in dispatch("/cost", ctx).message
    assert "30 credits" in dispatch("/cost", ctx).message


def test_slot_ctx_without_slots_keeps_pre_0336_behaviour():
    mgr = _slots((1000, 25))
    ctx = _slot_ctx(mgr.slots[0], logged_in=True)     # no slots= passed
    assert (ctx.session_tokens, ctx.session_credits) == (0, 0)
    # a real session slot always reports its OWN counters
    sess = mgr.slots[1]
    sess.sink.session_tokens, sess.sink.session_credits = 1000, 25
    ctx2 = _slot_ctx(sess, logged_in=True, slots=mgr)
    assert (ctx2.session_tokens, ctx2.session_credits) == (1000, 25)


def test_format_sessions_plain_marks_this_device():
    rows = [{"label": "terminal · macbook", "last_seen_at": "2026-07-26T00:10:00Z",
             "current": True},
            {"surface": "telegram", "last_seen_at": "2026-07-25T20:00:00Z"}]
    out = _format_sessions_plain(rows)
    assert "1. terminal · macbook" in out and "← this device" in out
    assert "2. telegram" in out
    assert _format_sessions_plain([]) == "Active sessions: (none)"


def test_home_offers_sign_in_when_signed_out():
    view = _view(HomeData(version="0.3.36"), _slots())
    body = "\n".join(_text(ln) for ln in view._all_lines())
    assert "not signed in" in body
    assert "Sign in to Imperal" in body and "/login" in body


def test_sign_in_item_is_absent_when_the_action_is_not_wired():
    """Pre-0.3.36 callers pass no sign_in -- the item must simply not exist."""
    noop = lambda *a, **k: None
    actions = HomeActions(new_session=noop, open_recent=noop, switch_tab=noop,
                          close_tab=noop, set_tab_mode=noop, set_notify=noop,
                          set_new_tab_mode=noop, top_up=noop,
                          open_security_docs=noop)
    model = build_home_model(HomeData(), [TabRow(1, "t", "default", "•", 0, 0, True)],
                             actions)
    assert not [i for i in model.items if i.id == "sign-in"]


# --------------------------------------------------------------------------
# 6. END-TO-END: the newline chords through a REAL dock + real key parsing
# --------------------------------------------------------------------------
def test_newline_chords_e2e_insert_instead_of_sending():
    """Drive `run_session` with a pipe input and real escape sequences.

    Alt+Enter (ESC CR), Ctrl+J (LF) and Shift+Enter (CSI-u ESC [13;2u) must
    each insert a newline and send NOTHING; the following Enter must submit
    the whole multi-line message as ONE line. This is the actual user-visible
    contract of the feature, exercised through prompt_toolkit's own parser.
    """
    import asyncio
    import time
    from types import SimpleNamespace

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from webbee import tui
    from tests.test_tui import mk_slots

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran = []

        async def on_line(text, slot=None):
            ran.append(text)

        sink = SimpleNamespace(
            status=lambda: {"tokens": 0, "credits": 0, "busy": False, "current": "",
                            "elapsed": 0.0, "tools": 0, "consent": False},
            is_busy=lambda: False, consent_pending=lambda: False,
            resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink)

        async def _until(pred, timeout=5.0):
            t0 = time.time()
            while not pred():
                assert time.time() - t0 < timeout, f"timed out; ran={ran}"
                await asyncio.sleep(0.01)

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.05)

                pipe.send_text("line one")
                pipe.send_text("\x1b\r")          # Alt+Enter  -> newline
                pipe.send_text("line two")
                pipe.send_text("\n")              # Ctrl+J     -> newline
                pipe.send_text("line three")
                pipe.send_text("\x1b[13;2u")      # Shift+Enter -> newline
                pipe.send_text("line four")
                await asyncio.sleep(0.15)
                assert ran == [], f"a newline chord SENT the line: {ran}"

                pipe.send_text("\r")              # Enter -> submit it all
                await _until(lambda: len(ran) == 1)
                assert ran[0] == "line one\nline two\nline three\nline four"

                pipe.send_text("\x04")            # Ctrl-D exits (idle)
                ok = await asyncio.wait_for(task, 5)
        assert ok is True

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# 7. the toolbar must never be cut mid-word on a narrow terminal
# --------------------------------------------------------------------------
def test_toolbar_drops_hints_before_numbers_when_narrow():
    """The spend figures are DATA, the key hints are discoverability -- so on a
    narrow terminal the hints go and the numbers stay (an 80-column window used
    to slice the line mid-word)."""
    def txt(width):
        return "".join(t for _, t in build_toolbar("autopilot", 128000, 3400, width=width))

    wide = txt(120)
    assert "Shift + TAB" in wide and "Alt+↵ newline" in wide

    at80 = txt(80)
    assert len(at80) <= 80
    assert "128k tok · 3.4k credits this session" in at80   # numbers survive
    assert "Alt+↵ newline" in at80                          # the newline hint survives
    assert "Shift + TAB" not in at80                        # the longer hint went

    tiny = txt(60)
    assert len(tiny) <= 60
    assert "128k tok · 3.4k credits this session" in tiny   # numbers STILL survive
    assert "newline" not in tiny


def test_toolbar_without_width_is_byte_identical_to_pre_0336_callers():
    """`width` is optional: omitted (or 0 = headless/unknown) keeps the full
    line, so every existing caller and test is unaffected."""
    assert build_toolbar("plan", 10, 2) == build_toolbar("plan", 10, 2, width=0)


def test_busy_and_consent_toolbars_ignore_width():
    """Only the idle line is width-managed; the busy/consent states are short
    already and must not change shape."""
    assert build_toolbar("default", 1, 1, consent=True, width=40) == \
        build_toolbar("default", 1, 1, consent=True)
    assert build_toolbar("default", 1, 1, busy=True, current="x", width=40) == \
        build_toolbar("default", 1, 1, busy=True, current="x")


# --------------------------------------------------------------------------
# 8. the multi-line prompt RENDERS as one message (continuation gutter)
# --------------------------------------------------------------------------
def test_multiline_prompt_renders_with_a_continuation_gutter():
    """A real dock render: line 1 keeps the coloured `❯ `, line 2+ get the dim
    `┊ ` gutter, and nothing is submitted until Enter."""
    import asyncio
    from types import SimpleNamespace

    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition
    from prompt_toolkit.output import DummyOutput

    from tests.test_tui import mk_slots
    from webbee import tui

    async def scenario():
        pane = tui.OutputPane(width=80)
        ran = []

        async def on_line(text, slot=None):
            ran.append(text)

        sink = SimpleNamespace(
            status=lambda: {"tokens": 0, "credits": 0, "busy": False, "current": "",
                            "elapsed": 0.0, "tools": 0, "consent": False},
            is_busy=lambda: False, consent_pending=lambda: False,
            resolve_consent=lambda t: None)
        slots = mk_slots(pane=pane, sink=sink)

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(tui.run_session(
                    slots=slots, on_line=on_line, on_cycle=lambda: None))
                await asyncio.sleep(0.1)
                pipe.send_text("first\x1b\rsecond")      # Alt+Enter between them
                await asyncio.sleep(0.15)

                screen = Screen()
                get_app().layout.container.write_to_screen(
                    screen, MouseHandlers(), WritePosition(0, 0, 80, 24), "", False, 0)
                rows = ["".join(screen.data_buffer[y][x].char for x in range(80)).rstrip()
                        for y in range(24)]
                first = next(r for r in rows if "first" in r)
                second = next(r for r in rows if "second" in r)
                assert "❯ first" in first
                assert "┊ second" in second
                assert ran == []                          # still composing

                pipe.send_text("\r")
                await asyncio.sleep(0.15)
                assert ran == ["first\nsecond"]

                pipe.send_text("\x04")
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
