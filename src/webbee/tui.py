"""Full-screen dock: a scrollable, colored output pane (Rich → ANSI, see
output_pane.py) fills the top; a bordered input box + toolbar are pinned at
the very bottom and never move while the output scrolls (mouse wheel /
PageUp). `run_session` also drives step-navigation (Up/Down + Enter) over the
pinned box when the input is empty and no turn is running; in every other
state Up/Down recall submitted lines (readline-style), and Enter while a turn
runs FLIES the line into the RUNNING turn (mid-turn inject, 0.3.15 — the
kernel absorbs it at the next brain step; its task_queued[terminal] echo
shows the panel row) with the local type-ahead queue as the fallback when no
inject leg is wired or it fails (shown LIVE in the queue panel pinned above
the input — see queue_panel.py — counted in the toolbar, run after the
current turn — natural completion only, a user STOP preserves the queue;
↑ on an empty input pulls the newest queued line back for editing, a click
pulls that item; /queue lists it, /queue clear drops it; the transcript
stays clean — real turns only). A STICKY todo panel (todo_panel.py) sits
above the queue panel and tracks the current checklist live. Pure helpers
(next_mode/build_toolbar/the *_action functions) are unit-tested; the
Application is TTY/headless-smoke verified. Grounded in prompt_toolkit
3.0.52."""
import asyncio
import re

from webbee import __version__, sizing
from webbee.output_pane import OutputPane  # noqa: F401 — re-exported (webbee.tui.OutputPane)
from webbee.queue_panel import (drop_item, one_line, pull_item, queue_fragments,
                                 queue_height)
from webbee.render import _fmt_tokens
from webbee.home_view import session_totals
from webbee.slots import close_active, close_at, disarm_all, is_turn_alive
from webbee.tabs import tab_fragments
from webbee.todo_panel import todo_fragments, todo_height

_MODES = ("default", "plan", "autopilot")
_TIERS = ("webbeesmart", "supersmart", "ultrasmart")
# webbee-code-tier-colors-v1: the wire/storage value stays lowercase
# (tier_store.py, the kernel's MODEL_TIERS, /model's own argument parsing —
# none of that changes), this is ONLY the human-facing label shown in the
# toolbar and in the switch confirmation note.
_TIER_DISPLAY = {"webbeesmart": "Smart", "supersmart": "SuperSmart", "ultrasmart": "UltraSmart"}
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille frames — animated while a turn runs

# webbee-code-tier-shimmer-v1 (Valentin, live 2026-07-31: "при смене модели в
# тулбаре никакой анимации нету, он как-то поломанно выглядит... хочу чтобы
# ТОЛЬКО сама модель... переливалась или слегка светилась"): the old
# tier-switch feedback was `pane.flash_note(...)` — it swapped the ENTIRE
# toolbar (mode, spend, hints, everything) for a static string for 1.5s, then
# snapped back — exactly the "поломанно" jank being reported. Replaced with a
# PERMANENT, gentle per-character colour sweep on the tier segment ONLY —
# nothing else in the toolbar ever changes because of it. Each tier keeps its
# OWN existing hue family (tb.tier.* below) so a glance still tells tiers
# apart; this just makes that one word visibly alive.
_TIER_GLOW = {
    "webbeesmart": ("#2f7a66", "#5fd7af", "#c8fff0"),
    "supersmart": ("#33499e", "#5f87ff", "#d3e0ff"),
    "ultrasmart": ("#9c2f95", "#ff5fd7", "#ffddf7"),
}


def _lerp_hex(c1: str, c2: str, frac: float) -> str:
    """PURE. Linear-interpolate two '#rrggbb' colours by frac (clamped to
    [0, 1]) — the smooth-gradient primitive the tier shimmer is built on."""
    frac = 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = round(r1 + (r2 - r1) * frac)
    g = round(g1 + (g2 - g1) * frac)
    b = round(b1 + (b2 - b1) * frac)
    return f"#{r:02x}{g:02x}{b:02x}"


def _tier_shimmer_fragments(tier: str, label: str, *, now: "float | None" = None) -> list:
    """PURE given `now`. A slow, gentle per-character brightness wave travels
    left-to-right across the model-tier name — a real gradient (smoothly
    interpolated colour, not a blunt whole-word flip), one full sweep every
    4s so it reads as a calm shimmer/glow even sampled at the idle ticker's
    1Hz rate, never a fast flicker. Returns one (style, char) fragment per
    character so prompt_toolkit renders each with its own instantaneous
    colour along the gradient."""
    import time as _t
    if now is None:
        now = _t.monotonic()
    colors = _TIER_GLOW.get(tier, _TIER_GLOW[_TIERS[0]])
    n = len(colors)
    period = 4.0
    frags = []
    for i, ch in enumerate(label):
        # per-character phase offset -> the bright band visibly TRAVELS
        # across the word instead of the whole word pulsing in lock-step.
        phase = ((now / period) + i * 0.18) % 1.0
        tri = phase * 2 if phase < 0.5 else (1.0 - phase) * 2   # 0 -> 1 -> 0
        pos = min(tri, 1.0) * (n - 1)
        idx = min(int(pos), max(n - 2, 0))
        frac = pos - idx
        color = _lerp_hex(colors[idx], colors[min(idx + 1, n - 1)], frac)
        frags.append((f"fg:{color}", ch))
    return frags

# Leaked SGR mouse-report fragments ("<35;6;42M" / "35;6;42M"): under a
# mouse-move flood the vt100 parser splits sequences at read-chunk boundaries
# and the printable tail lands in the input buffer as literal text (live on
# Linux + occasionally macOS, 2026-07-12). Requires the full x;y;btn+M shape —
# ordinary "a;b;c" text never matches (a literal "35;6;42M" the user typed
# would be dropped too; accepted, it IS the residue shape).
_MOUSE_RESIDUE = re.compile(r"(?:\x1b\[)?<?\d{1,4};\d{1,4};\d{1,4}[Mm]")
# 0.3.25: stray DEC focus-in/out reports ("\x1b[I" / "\x1b[O") — same split-
# sequence hazard as the mouse residue above, but from ANOTHER source (tmux/
# a window manager still sending them even though configure_mouse_modes now
# explicitly turns ?1004 off — see its own docstring). ESC-PREFIXED ONLY: a
# bare "[I"/"[O" with no leading ESC is ordinary text (e.g. "see [I]" in a
# citation) and must never be eaten.
_FOCUS_RESIDUE = re.compile(r"\x1b\[[IO]")


def scrub_mouse_residue(text: str) -> str:
    """PURE. Drop leaked mouse-report AND focus-report fragments (0.3.25);
    everything else unchanged."""
    text = _MOUSE_RESIDUE.sub("", text or "")
    return _FOCUS_RESIDUE.sub("", text)


_STYLE_DICT = {
    "frame.border": "#5f5f5f",           # muted grey chrome — furniture, not focus
    "prompt": "#00afd7 bold",            # cyan ❯ — the interactive accent
    "tabbar": "bg:#262626",              # 0.3.25: the bar itself — a browser-look strip the chips sit on
    "tab": "#9e9e9e",                    # idle chip — dim text, no bg (brightened a notch, 0.3.25, to read clearly on `tabbar`'s bg)
    "tab.active": "bg:#e8a317 #1c1c1c bold",  # the ACTIVE chip — solid bee-yellow bg, dark text: unmistakable
    "tab.alert": "#e8a317 bold",         # ⚠ consent waiting in a BACKGROUND tab — yellow text, no bg (only the active chip owns one); also the armed "✕?" busy-close-confirm glyph (0.3.25)
    "tab.close": "#9e9e9e",              # the ✕ on a background tab — dim, closing is never the default action (brightened alongside `tab`)
    "tab.close.active": "bg:#e8a317 #1c1c1c",  # the ✕ on the ACTIVE tab — same bg as its chip, reads as one contiguous block
    "tab.new": "#e8a317 bold",            # 0.3.26: bee-yellow + prominent (was #6f6f6f)
    "tab.sep": "#3a3a3a",                # the │ between tabs — dim, consistent, exactly one per pair, none at the ends
    "tb.dim": "#8a8a8a",                 # idle chrome / secondary bits — dim
    "tb.spin": "#e8a317 bold",           # animated spinner — bee-yellow, pops
    "tb.working": "#e8a317",             # 'working' — yellow
    "tb.action": "#00afd7",              # current action — cyan
    "tb.consent": "#e8a317 bold",        # consent prompt line — yellow
    "tb.mode.default": "#00afd7",        # default — cyan
    "tb.mode.plan": "#af87ff",           # plan — purple
    "tb.mode.autopilot": "#e8a317 bold", # autopilot — yellow (auto-approving: caution)
    # webbee-code-tier-colors-v2: FIXED -- v1 accidentally reused the exact
    # mode colours (cyan/purple/yellow), so tiers were visually identical to
    # modes. Genuinely distinct palette now, same "calm -> bold" progression
    # but no shared hex with tb.mode.*: webbeesmart=teal-green (calm baseline),
    # supersmart=soft blue-violet (a step up), ultrasmart=hot magenta bold
    # (the top tier -- meant to pop, but pop DIFFERENTLY from autopilot's
    # yellow caution so the two are never confusable at a glance).
    "tb.tier.webbeesmart": "#5fd7af",
    "tb.tier.supersmart": "#5f87ff",
    "tb.tier.ultrasmart": "#ff5fd7 bold",
    "tb.version": "#5f5f5f",             # 0.3.37 bottom-right version badge — quietest thing on screen
    "tb.fresh": "#5faf5f",               # 0.3.40 — badge: verified up to date (mirrors home.fresh)
    "tb.fresh.bright": "#87ff87 bold",    # webbee-code-badge-breathe-v1 — the brighter half-beat
                                           # of the "up to date" badge's breathing animation
    "tb.update": "#e8a317 bold",         # 0.3.40 — badge: a newer release exists (mirrors home.update)
    "qp.header": "#e8a317 bold",         # queue-panel header — bee-yellow, pops
    "qp.item": "#8a8a8a italic",         # older queued rows — muted (echoes grey66)
    "qp.last": "#e8a317",                # newest row — the one ↑ pulls
    "qp.remote": "#af87ff italic",       # cross-surface rows — purple (not yours to pull)
    "qp.drop": "#d75f5f",                # per-row ✕ remove button (0.3.37) — red, deliberate
    "tb.live": "#5fd75f",                # live-session indicator (0.3.37) — green = a workflow is Running
    "tp.header": "#e8a317 bold",         # todo-panel header — bee-yellow, pops
    "tp.done": "#5faf5f",                # ✓ glyph — green
    "tp.done.text": "#8a8a8a strike",    # completed text — dim + struck
    "tp.now": "#e8a317 bold",            # ▶ current item — bee-yellow, always pops
    "tp.item": "#8a8a8a",                # pending rows / overflow — muted
    # W5 interactive Home dashboard
    "home.header": "#e8a317 bold",
    "home.value": "#ffffff bold",
    "home.item": "#00afd7",
    "home.dim": "#8a8a8a",
    "home.disabled": "#5f5f5f",
    "home.focus": "bg:#e8a317 #1c1c1c bold",
    "home.hint": "#00afd7",
    "home.update": "#e8a317 bold",       # a newer release exists — bee-yellow, pops
    "home.fresh": "#5faf5f",             # verified up to date — calm green
    "home.spend": "#ffffff bold",        # session spend total — reads as a figure
    "input.cont": "#5f5f5f",             # multi-line prompt continuation gutter
}


def configure_mouse_modes(output) -> None:
    """Replace prompt_toolkit's ANY-EVENT mouse tracking (?1003 — every bare
    mouse move fires a report) with BUTTON-EVENT tracking (?1002 — reports only
    while a button is held). Wheel scroll, clicks and drag-select all still
    work; the bare-move flood that desyncs the parser (phantom Escape + report
    tails typed into the input) disappears at the source. No-op for outputs
    without write_raw (non-vt100).

    0.3.25 (focus/garbage hardening): both paths ALSO explicitly disable
    DEC focus-reporting (?1004l) — a tmux pane switch, an OS-level window
    focus change, or another program that left ?1004 armed can otherwise
    leak `ESC[I`/`ESC[O` focus-in/out reports straight into THIS terminal's
    stdin, landing as garbage in the input buffer exactly like the mouse
    residue below. `_enable` turns it off the moment the dock's own mouse
    tracking comes up (so nothing else's focus reporting can leak for the
    whole session); `_disable` repeats it on teardown, belt & braces, same
    posture as ?1003 above."""
    if not hasattr(output, "write_raw"):
        return

    def _enable():
        output.write_raw("\x1b[?1000h")   # clicks + wheel
        output.write_raw("\x1b[?1002h")   # motion ONLY while a button is held
        output.write_raw("\x1b[?1015h")   # urxvt encoding
        output.write_raw("\x1b[?1006h")   # SGR encoding
        output.write_raw("\x1b[?1004l")   # focus reporting OFF -- never wanted here

    def _disable():
        output.write_raw("\x1b[?1002l")
        output.write_raw("\x1b[?1003l")   # belt & braces: clear any-event too
        output.write_raw("\x1b[?1000l")
        output.write_raw("\x1b[?1015l")
        output.write_raw("\x1b[?1006l")
        output.write_raw("\x1b[?1004l")   # belt & braces: focus reporting stays off on exit too

    output.enable_mouse_support = _enable
    output.disable_mouse_support = _disable


def enable_csi_u_newline() -> bool:
    """Teach prompt_toolkit's vt100 parser the CSI-u encodings of Shift+Enter.

    prompt_toolkit 3.0.52 has NO ShiftEnter key: in a legacy terminal
    Shift+Enter is byte-identical to Enter (CR), so it CANNOT be bound.
    Modern terminals (kitty/foot/WezTerm/Ghostty, xterm with modifyOtherKeys,
    iTerm2 with CSI-u) instead emit `ESC [13;<mod>u`. Registering those
    sequences as (Escape, ControlM) makes them arrive as the SAME two-key
    sequence Alt+Enter already produces, so ONE key binding -- ("escape",
    "enter") -- serves both chords and nothing else in the app changes.

    Registration is additive and idempotent: existing sequences are never
    overwritten, so no other key can regress. Returns True when the table
    now contains the codes (best effort -- never raises, an old/patched
    prompt_toolkit just means Alt+Enter and Ctrl+J remain the way in).
    """
    try:
        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys
    except Exception:
        return False
    combo = (Keys.Escape, Keys.ControlM)
    # 13 = Enter's unicode codepoint (CR) in the CSI-u scheme; modifier code
    # 2 = Shift, 4 = Alt(Meta), 6 = Ctrl+Shift, 8 = Ctrl+Alt+Shift.
    for mod in (2, 4, 6, 8):
        seq = f"\x1b[13;{mod}u"
        try:
            ANSI_SEQUENCES.setdefault(seq, combo)
        except Exception:
            return False
    try:
        # The parser memoises prefix lookups built from ANSI_SEQUENCES; drop
        # the cache so sequences registered after import are actually seen.
        from prompt_toolkit.input import vt100_parser as _vp
        for _obj in vars(_vp).values():
            if hasattr(_obj, "cache_clear"):
                _obj.cache_clear()
    except Exception:
        pass
    return True


def input_rows(text: str, cols: int, cap: int) -> int:
    """PURE row-wrap estimator behind `_input_height` (module-level so tests
    drive it directly with an injected size, mirroring repl._gate_busy).
    Same wrap math as before the W2 proportional-sizing pass: `cols` is the
    usable wrap width (frame + prompt already subtracted by the caller,
    floored at 10 so a tiny/misreported width never collapses every line to
    1-char rows); `cap` bounds growth (was: hardcoded 10, now the caller's
    live `sizing.input_height_cap(rows)` — the box may grow to at most a
    PROPORTION of the screen, not a fixed character count)."""
    if not text:
        return 1
    cols = max(10, cols)
    rows = sum(max(1, -(-len(ln) // cols)) for ln in text.split("\n"))
    return min(cap, max(1, rows))


def next_mode(mode: str) -> str:
    try:
        return _MODES[(_MODES.index(mode) + 1) % len(_MODES)]
    except ValueError:
        return _MODES[0]


def next_tier(tier: str) -> str:
    """PURE. Cycles webbeesmart -> supersmart -> ultrasmart -> webbeesmart. An unset/""
    tier (server admin default, never chosen) or any unrecognised value
    starts the cycle at the first tier, same fallback discipline as
    next_mode above."""
    try:
        return _TIERS[(_TIERS.index(tier) + 1) % len(_TIERS)]
    except ValueError:
        return _TIERS[0]


def build_toolbar(mode: str, tokens: int, credits: int, *, busy: bool = False,
                  current: str = "", elapsed: float = 0.0, tools: int = 0,
                  consent: bool = False, queued: int = 0,
                  reconnecting: int = 0, width: int = 0, live: str = "",
                  tier: str = "", tier_now: "float | None" = None) -> list:
    """The status line under the pinned input box, as prompt_toolkit formatted
    text (per-segment styled). Four states: consent (awaiting a reply),
    reconnecting (the stream transport is down mid-turn — honest, not a fake
    spinner: the run continues server-side and resumes on reconnect), busy
    (a turn is running — an ANIMATED coloured spinner + the current action in
    accent, so it pops, not grey), and idle (mode value coloured PER MODE —
    default cyan / plan purple / autopilot yellow — + SESSION spend + the
    Shift + TAB hint). `queued` = type-ahead lines waiting to run after the
    current turn; when >0 the `⋯N queued` segment renders in the ACCENT class
    (tb.working, NOT dim) in the busy AND idle states, so the depth is
    noticeable at a glance. Style classes are defined in run_session's Style."""
    q = [("class:tb.working", f" · ⋯{queued} queued")] if queued else []
    if consent:
        return [("class:tb.consent", "  approve? type y / n / a reply · Enter to send")]
    if busy and reconnecting:
        frags = [("class:tb.consent", f"  ⟳ reconnecting ({reconnecting})"),
                 ("class:tb.dim", " · the run continues server-side")]
        frags += q
        frags.append(("class:tb.dim", "   ·   Esc/Ctrl-C to stop"))
        return frags
    if busy:
        spin = _SPINNER[int(elapsed * 10) % len(_SPINNER)]   # animates via the ticker
        frags = [("class:tb.spin", f"  {spin} "), ("class:tb.working", "working")]
        if current:
            frags += [("class:tb.dim", " · "), ("class:tb.action", current)]
        # webbee-code-model-tier-toolbar-v1: keep the tier visible mid-turn too
        # (a short "· SuperSmart" tag, NOT the full "model: " label used at
        # idle -- busy is already the densest row, this is the cheapest
        # legible form). webbee-code-tier-colors-v1: each tier gets its own
        # colour + its human display name (Smart/SuperSmart/UltraSmart),
        # never the raw wire value.
        # webbee-code-model-selector-always-visible-v1 (Valentin, live
        # 2026-07-31: "я в КАЖДОЙ вкладке явно хочу видеть какая модель, а
        # не system default"): an unset tier now shows the REAL name of the
        # tier that actually runs when none is chosen -- "webbeesmart" IS the
        # documented default, fast everyday tier (imperal-ext-admin's own
        # Model Tiers panel, `_TIERS[0]`), so showing its display name
        # "Smart" here is a grounded fact about what's running, not a
        # guess -- never a MADE-UP tier the server didn't confirm.
        frags += [("class:tb.dim", " · ")]
        frags += _tier_shimmer_fragments(tier or _TIERS[0],
                                         _TIER_DISPLAY.get(tier, "") or _TIER_DISPLAY[_TIERS[0]],
                                         now=tier_now)
        frags.append(("class:tb.dim",
                      f" · {elapsed:.0f}s · {tools} · {_fmt_tokens(tokens)} tok"))
        frags += q
        frags.append(("class:tb.dim", "   ·   Esc/Ctrl-C to stop"))
        return frags
    frags = [("class:tb.dim", "  mode: "),
             (f"class:tb.mode.{mode}", mode)]
    # webbee-code-model-tier-toolbar-v1: the tier was only ever visible via
    # `/model` (no arg) or right after a Ctrl+B cycle flash -- ask once, then
    # forget, exactly the visibility gap the mode segment right next to it
    # never had.
    # webbee-code-model-selector-always-visible-v1 (Valentin, live
    # 2026-07-31: "я в КАЖДОЙ вкладке явно хочу видеть какая модель, а не
    # system default"): every tab must show a model indicator, chosen or
    # not -- "" (unset) shows the REAL name of the default tier that's
    # actually active ("Smart" / webbeesmart, imperal-ext-admin's own
    # documented default), never a vague placeholder phrase. Ctrl+B /
    # `/model` still change it from here exactly like before.
    frags += [("class:tb.dim", " · model: ")]
    frags += _tier_shimmer_fragments(tier or _TIERS[0],
                                     _TIER_DISPLAY.get(tier, "") or _TIER_DISPLAY[_TIERS[0]],
                                     now=tier_now)
    frags += [("class:tb.dim", f"   ·   {_fmt_tokens(tokens)} tok · {_fmt_tokens(credits)} credits this session"),
             *q]
    # 0.3.37: the PERSISTENT live-session indicator (active_sessions.
    # session_indicator). Empty string = nothing running = say nothing, so an
    # ordinary idle dock gains no permanent noise; when a Temporal workflow IS
    # running the terminal now says so continuously instead of once in a boot
    # note that scrolls away.
    if live:
        frags.append(("class:tb.live", f"   ·   {live}"))
    # 0.3.36: the hints are DISCOVERABILITY, the spend figures are DATA -- so
    # the hints are what gets dropped when the terminal is too narrow to hold
    # everything, never the numbers (an 80-column window used to cut the line
    # mid-word). `width=0` (unknown/headless) keeps the full line, which is
    # also what every pre-0.3.36 caller and test sees.
    used = sum(len(t) for _, t in frags)
    # Rotating tips without blinking: calm 8-second cycle across useful platform tips,
    # always preserving essential shortcuts (Alt+↵ newline & Shift + TAB: switch mode).
    import time as _t_tips
    _tip_cycle = int(_t_tips.monotonic() // 8) % 4
    if _tip_cycle == 0:
        full = "   ·   Alt+↵ newline · Shift + TAB: switch mode · Ctrl+B: model tier"
    elif _tip_cycle == 1:
        full = "   ·   Alt+↵ newline · Shift + TAB: switch mode · Ctrl+T: new tab"
    elif _tip_cycle == 2:
        full = "   ·   Alt+↵ newline · Shift + TAB: switch mode · /cost: spend"
    else:
        full = "   ·   Alt+↵ newline · Shift + TAB: switch mode · /status: system"
    mid = "   ·   Alt+↵ newline · Shift + TAB: switch mode"
    short = "   ·   Alt+↵ newline"

    if not width or used + len(full) <= width:
        frags.append(("class:tb.dim", full))
    elif used + len(mid) <= width:
        frags.append(("class:tb.dim", mid))
    elif used + len(short) <= width:
        frags.append(("class:tb.dim", short))
    return frags


def version_badge_text(version: str) -> str:
    """PURE. The exact bottom-right badge string. ONE source of truth shared by
    `pin_version_right` (which draws it) and `_toolbar` (which reserves its
    columns before the hint text is fitted) -- so the two can never disagree
    about how much room the badge needs."""
    return f" v{version} "


def pin_version_right(frags: list, version: str, width: int, *,
                      notice: str = "", checked: "bool | None" = None,
                      style_override: str = "", text_override: str = "") -> list:
    """PURE. Pin the version badge flush to the RIGHT EDGE of the toolbar row.

    The toolbar is the LAST child of run_session's root HSplit, so its right
    edge is the bottom-right corner of the WHOLE window (0.3.37 — the badge
    used to sit inline in the idle text, which meant it moved, and vanished
    the moment a turn started).

    Applied ONCE over the already-built fragments in ``_toolbar()``, so every
    state (idle / busy / consent / reconnecting / copy-flash / step-nav /
    Home) carries it with no per-branch duplication — and, since 0.3.40, on
    EVERY tab (not just Home): ``checked``/``notice`` come from ONE
    process-wide PyPI check run at boot (`update.check_update_status`,
    24h-cached), threaded in as `update_state` all the way from `run_repl`.

    Three honest states, exactly `home_view.version_badge`'s own three (the
    SAME verdict, SAME three colours, now shown in the one place every tab
    can always see — Home's own in-content copies are gone, this replaces
    them):
      * ``checked is None``  -- the background check hasn't resolved yet
        (still in flight, or this call site never wired one in): the quiet
        old plain ``v<version>`` in the dim `tb.version` class -- NO claim
        about freshness either way.
      * ``checked is True``  -- resolved: `home_view.version_badge`'s own
        text/style ("v0.3.39 · up to date" dim-green, or "v0.3.38 →
        0.3.39 available" bee-yellow) reused verbatim -- one source of
        truth for the wording AND the colour, never a second copy drifting.
      * ``checked is False`` -- we tried and could not reach PyPI (offline):
        same bare ``v<version>``, no false reassurance.

    Contract (unchanged from 0.3.37):
      * ``width <= 0`` (headless / unknown terminal) -> returned UNCHANGED:
        padding to an unknown width would wrap the row and shove the input
        box upward.
      * The badge is dropped when the row cannot hold it plus at least one
        column of real content. The status line is DATA, the version is
        decoration -- decoration never truncates data. A long "update
        available" badge that doesn't fit degrades to being dropped
        entirely, same as before -- never a truncated, confusing partial.
      * Padding is exact (``width - used - len(badge)``) so the badge lands
        hard against the right edge and can never wrap to row two.
    """
    if not width or width <= 0:
        return frags
    if checked is None:
        text, style = version_badge_text(version).strip(), "tb.version"
    else:
        from webbee.home_view import version_badge
        raw, cls = version_badge(version, notice, checked=checked)
        text, style = raw, ("tb.fresh" if cls == "class:home.fresh" else
                            "tb.update" if cls == "class:home.update" else "tb.version")
    # webbee-code-badge-cycle-v1: caller may override the TEXT ITSELF too
    # (Valentin, live 2026-07-31: the badge must visibly ALTERNATE between
    # "v0.X.Y" and "up to date" every ~5s -- proof the freshness check is a
    # live thing, not a static line -- only in the one honest state where
    # both halves are true at once (checked, nothing newer). The caller
    # (`_badge_cycle_text`) already gates this to that exact state, so this
    # function stays a dumb, pure swap with no state of its own.
    if text_override:
        text = text_override
    # webbee-code-badge-breathe-v1: caller may override the STYLE CLASS only
    # (never the text/length -- the row width contract above is unchanged)
    # to make the "up to date" badge visibly breathe between two shades.
    if style_override:
        style = style_override.removeprefix("class:")
    badge = f" {text} "
    used = sum(len(t) for _, t in frags)
    pad = width - used - len(badge)
    if pad < 1:
        return frags
    return list(frags) + [("class:tb.dim", " " * pad), (f"class:{style}", badge)]


def _width_watch(pane, app) -> None:
    """Per-tick resize detector (W2 front-2): prompt_toolkit repaints on
    SIGWINCH by itself, but the RICH side (console width) must be told to
    re-wrap — this is the bridge. Two int compares when nothing changed.
    Swallows any reflow error — the ticker is the dock's only animation
    loop (spinner + queue drains ride on it too) and must never die.

    DEBOUNCED (2026-07-25): `pane.reflow` re-renders the whole retained ring,
    which is linear in records (~29µs each — ~116ms at the 4000-record cap)
    and runs ON the event loop, so the dock is frozen for its whole duration.
    Reflowing on the FIRST tick that sees a new width meant a slow drag-resize
    paid that cost once per intermediate width the ticker happened to sample
    (measured: 5 samples ≈ 535ms of cumulative freeze) — and every one of
    those reflows was thrown away by the next sample anyway.

    So a changed width is only REMEMBERED on the tick that first sees it; the
    reflow fires on the next tick that reads the SAME width, i.e. once the
    user stops dragging. `_resize_pending` lives on the pane (not module
    state) so each tab debounces its own resize independently, and
    `_ticker_busy` treats a pending resize as busy so the settle happens at
    the fast 0.25s cadence instead of the 1.0s idle one."""
    from webbee.sizing import get_size
    cols, _rows = get_size(app)
    if not cols:
        return
    pending = getattr(pane, "_resize_pending", 0)
    if cols != pane.console.width:
        if cols != pending:
            pane._resize_pending = cols     # still moving — settle next tick
            return
        pane._resize_pending = 0
        try:
            pane.reflow(cols)
        except Exception:
            pass
    elif pending:
        pane._resize_pending = 0            # snapped back to the current width


def _ticker_busy(slots, is_busy) -> bool:
    """Whether the dock's animation loop must stay at the FAST (0.25s) cadence:
    a turn is running, a copy-flash toast is live, an edge-drag auto-scroll is
    in flight (`pane._edge_drag` — a drag-select past the viewport edge that
    `pane.edge_tick()` keeps scrolling every tick), a debounced resize is
    waiting to settle (`pane._resize_pending`, see `_width_watch`), OR a
    tier-switch glow window is open (webbee-code-tier-shimmer-v1:
    `pane.tier_glow()` — the few seconds right after Ctrl+B/`/model` actually
    changes tiers, so the colour sweep animates smoothly instead of stepping
    once a second). All of them need smooth ~4x/s updates; miss the
    edge-drag one and idle drag-scrolling crawls 4x slower, miss the resize
    one and a re-wrap after a drag lands up to a full second late. Otherwise
    the loop is idle → the caller uses the slow 1.0s cadence."""
    try:
        pane = slots.active().pane
        if (bool(pane.flash()) or bool(getattr(pane, "_edge_drag", 0))
                or bool(getattr(pane, "_resize_pending", 0))
                or bool(getattr(pane, "tier_glow", lambda: False)())):
            return True
    except Exception:
        pass
    return bool(is_busy())


def _tick_interval(busy: bool) -> float:
    """The dock's animation-loop sleep. 0.25s while a turn is running (or a
    copy-flash is live) so the spinner/elapsed-clock animate smoothly; 1.0s
    when fully idle, so the loop wakes 1×/s instead of 4×/s (less CPU/battery)
    with no visible cost — an idle tick only resize-detects + re-syncs hover,
    both of which tolerate a 1s lag (and a turn's first frame still shows
    instantly via the submit's own invalidate)."""
    return 0.25 if busy else 1.0


def _tick_once(slots, app, is_busy, breathing=None) -> None:
    """One iteration of run_session's `_ticker` loop, extracted module-level
    so the wiring itself is directly unit-testable (an `async def` infinite
    loop otherwise only proves itself by running the whole dock). W4a Task 3:
    takes the SlotManager, not a pane — `slots.active().pane` is resolved
    HERE, every tick, so a tab switch immediately redirects the ticker at the
    newly-visible slot's own pane (its edge-drag, its resize-reflow) with no
    stale reference left over from the slot that was active a moment ago.
    Three effects, in order: (1) `_width_watch` — resize-detect + reflow
    bridge, UNCONDITIONAL busy or idle; (2) `pane.edge_tick()` — repeat-scroll
    while parked at a drag edge, error-swallowed so a broken edge-tick can
    never kill the dock's only animation loop; (3) `app.invalidate()` exactly
    when a turn is running OR the copy-flash toast is still fresh, so the
    spinner/elapsed-clock/flash-expiry all animate without redrawing on every
    idle tick for nothing."""
    active = slots.active()
    pane = active.pane
    _width_watch(pane, app)
    try:
        pane.edge_tick()
    except Exception:
        pass
    # webbee-code-badge-breathe-v1: `breathing` (optional) reports whether the
    # bottom-right version badge is in its "up to date, pulse the colour"
    # state right now -- if so the idle 1.0s-cadence tick still has to
    # actually REDRAW so the alternating shade is ever seen, not just
    # computed and silently discarded on every tick the same as a no-op one.
    # webbee-code-home-live-ages-v1: Home's own tab-list ages/durations are
    # computed FRESH every render (home_view.tab_rows reads time.monotonic()
    # each call) -- but a render only happens on an app.invalidate(), and the
    # busy/flash/breathing checks above are ALL false while you're simply
    # sitting on Home doing nothing. That froze every age at whatever it was
    # the moment you switched to Home -- looking like one shared, wrong
    # number instead of each tab's own live-ticking age. Home is cheap to
    # redraw (same virtualized-slice render every other pane already does),
    # so it gets the plain 1s idle cadence's invalidate too, unconditionally.
    # webbee-code-tier-shimmer-v1: the tier segment's colour sweep is driven
    # by wall-clock time inside build_toolbar -- but it must NOT force a
    # permanent redraw on every idle session tab (that would defeat the
    # dock's whole idle-CPU-~0 discipline, see
    # test_healthy_idle_ticker_does_not_repaint). Instead `pane.tier_glow()`
    # reports a short (~6s) TRUE window right after a tier actually changes
    # (armed by _tier_cycle/the /model action) -- the fast cadence kicks in
    # for just that "wow, something changed" burst, exactly like the existing
    # copy-flash toast already does, then idle goes back to costing nothing.
    if (is_busy() or pane.flash() or (breathing is not None and breathing())
            or getattr(active, "kind", "") == "home"
            or bool(getattr(pane, "tier_glow", lambda: False)())):
        app.invalidate()


def _forwarding(handler, pane):
    """W2 Task 8: prompt_toolkit routes mouse events by pointer POSITION,
    not by who owns an in-progress drag, so a selection armed inside the
    output pane needs its neighbor windows' own mouse handling to give it
    first refusal — otherwise a release past the pane's Window just lands on
    whatever's underneath and the drag never completes (stuck highlight,
    copy never fires). Wraps `handler` (a plain mouse_handler(ev), or None
    for a window that has no handler of its own — e.g. the toolbar) so
    `pane.forward_mouse(ev)` is tried FIRST: consumed (a drag was armed) ⇒
    stop here, return None; otherwise fall through to `handler(ev)`, or
    NotImplemented when there's no wrapped handler at all — the toolbar's
    case, where forwarding is the ONLY behavior being added."""
    def _h(ev):
        if pane.forward_mouse(ev):
            return None
        if handler is None:
            return NotImplemented
        return handler(ev)
    return _h


def _badge_click(pane, notice: str, forward):
    """0.3.40: the bottom-right version badge is now a real click target, not
    just decoration -- when a newer release is known (`notice` non-empty,
    the exact upgrade sentence `update.check_update_status` produced), a
    MOUSE_UP on the badge flashes it into the toolbar via `pane.flash_note`
    (the SAME transient-note mechanism copy-on-select already uses, so it
    reads as one consistent visual language rather than a second kind of
    popup). `forward` (the toolbar's own drag-forwarding wrapper) still gets
    first refusal, exactly like every other toolbar fragment -- a drag that
    happens to release on the badge must complete the copy, never fire the
    click instead."""
    def _h(ev):
        if forward(ev):
            return None
        from prompt_toolkit.mouse_events import MouseEventType
        if ev.event_type == MouseEventType.MOUSE_UP:
            pane.flash_note(notice, secs=6.0)
            return None
        return NotImplemented
    return _h


def _escape_action(sel: dict, turn: dict, is_busy, stop_turn, event, buf=None, sink=None) -> None:
    """Esc key binding (P5g). While a turn is running, STOP it — cancel the LOCAL
    turn task (what actually tears the turn down, same as Ctrl-C) AND ask the
    server to stop (so it stops spending). While idle, clear the step-selection.
    If a consent or confirmation prompt is pending, cancel it safely without hanging.

    Phantom-Esc guard (2026-07-12): a mouse-report flood splits sequences —
    the ESC arrives as a lone Escape KEY and the tail lands in the input
    buffer. Residue in the buffer ⇒ this Escape is almost certainly a split
    report, not the user: clean the buffer and KEEP the turn running."""
    if sink is not None:
        cc = getattr(sink, "cancel_consent", None)
        cp = getattr(sink, "consent_pending", None)
        if callable(cp) and cp() and callable(cc):
            cc("declined")
            event.app.invalidate()
            return
    if is_busy():
        if buf is not None and _MOUSE_RESIDUE.search(buf.text or ""):
            buf.text = scrub_mouse_residue(buf.text)
            return
        t = turn.get("task")
        if t is not None and not t.done():
            if stop_turn is not None:
                event.app.create_background_task(stop_turn())
            turn["stopped"] = True   # user STOP → the type-ahead queue must NOT auto-run
            t.cancel()                       # cancel the running turn; dock survives
        return
    sel["i"] = None
    event.app.invalidate()


def _interrupt_action(turn: dict, is_busy, stop_turn, event) -> None:
    """Ctrl-C key binding (P5g). The LOCAL task.cancel() is what actually
    tears the dock down (unchanged); ALSO ask the server to stop the turn so
    it doesn't keep running (and spending) after the dock moves on."""
    t = turn["task"]
    if t is not None and not t.done():
        if is_busy() and stop_turn is not None:
            event.app.create_background_task(stop_turn())
        turn["stopped"] = True   # user STOP → the type-ahead queue must NOT auto-run
        t.cancel()                          # cancel the running turn; dock survives


def _can_close_tab(buf, slot) -> bool:
    """PURE. Ctrl-W's filter predicate (Task 5) — same DI-testing philosophy
    as `_escape_action`/`_interrupt_action`, exposed module-level so a test
    drives it directly instead of only through a live Application. Ctrl-W
    closes the active tab ONLY when the input is empty (a non-empty draft
    means the user wants PT's normal word-delete, not a tab close) AND the
    active slot is an actual session (Home has nothing to close)."""
    return not buf.text and slot is not None and slot.kind == "session"


def _should_close_on_eof(slots) -> bool:
    """PURE. Ctrl-D's tab-vs-quit policy (Task 5): closing the active SESSION
    tab is the natural Ctrl-D action as long as at least one OTHER session
    tab survives — closing the last one instead falls through to the
    original behavior (exit the app when idle), since landing on a bare Home
    with nothing left open is close enough to "quit" that a second Ctrl-D
    finishes the job instead of a tab-close silently doing nothing new."""
    return slots.session_count() > 1 and slots.active().kind == "session"


class QueuedLine(str):
    """A locally-queued type-ahead line that remembers the steer_iid minted at
    enqueue time (mid-turn inject fallback, 0.3.15). A plain str everywhere it
    already flows (panel one_line, pull_item→buffer, history, dispatch) — the
    iid rides along ONLY so the turn-end drain re-submits under the SAME dedup
    id: if a failed-looking inject actually landed server-side, the kernel's
    steer-iid ring drops the twin instead of running it twice."""
    iid = ""

    def __new__(cls, text: str, iid: str = ""):
        s = str.__new__(cls, text)
        s.iid = str(iid or "")
        return s


async def _inject_or_queue(inject, text: str, pending, invalidate=None) -> bool:
    """Enter-while-busy fly-in (mid-turn inject, 0.3.15): mint the steer_iid
    HERE — one id for both legs — and POST immediately via `inject(text, iid)`
    (the repl's gateway leg). REUSE, don't mint, when `text` already carries
    one (a QueuedLine — a pull-to-edit resubmitted UNCHANGED, see
    _rewrap_pulled): the original inject may have already landed server-side,
    so re-flying it under the SAME iid lets the kernel ring dedup the twin
    instead of running a genuine duplicate turn. On ok the line is
    KERNEL-owned: nothing is queued locally — the kernel's
    task_queued{origin:terminal} echo renders the panel row and
    task_dequeued clears it when the running turn absorbs it (seconds, the
    next brain step). On ANY failure (no live session yet, offline, gateway
    refusal) fall back to today's local queue: the row — carrying the SAME
    iid — drains at turn end through _drain_pending, stays ↑/click-pullable,
    and the kernel ring dedups the twin if the inject landed after all.
    Returns True when the line flew in (the caller's tests read it)."""
    from uuid import uuid4
    iid = getattr(text, "iid", "") or uuid4().hex
    try:
        ok = bool(await inject(text, iid))
    except Exception:
        ok = False
    if not ok:
        pending.append(QueuedLine(text, iid))
    if invalidate is not None:
        invalidate()
    return ok


def _submit_line(text: str, buf, pending, busy: bool, start, inject=None) -> str:
    """Route ONE submitted line (dependency-injected, same testing philosophy
    as _escape_action). Whitespace never queues nor starts ("ignored"). A real
    line is recorded into the buffer's history first (up-arrow recall), then:
    busy + an `inject` launcher wired → fly it into the RUNNING turn NOW
    (mid-turn inject, 0.3.15 — the launcher fires _inject_or_queue as a
    background task; a failed inject falls back to the local queue there);
    busy without a launcher → QUEUE it (Claude-Code type-ahead — the line is
    never erased or dropped; it runs after the current turn) — the LIVE queue
    panel above the input shows it at once (queue_panel.queue_fragments reads
    this deque every redraw), NEVER a static scrollback echo (those scrolled
    away, duplicated and went stale when edited); idle → `start(text)`
    (today's normal submit, unchanged).
    Returns "ignored" | "injected" | "queued" | "started"."""
    if not text.strip():
        return "ignored"
    buf.history.append_string(text)
    if busy:
        if inject is not None:
            inject(text)
            return "injected"
        pending.append(text)
        return "queued"
    start(text)
    return "started"


def _drain_pending(pending, start, mark=None) -> bool:
    """Turn-completion drain: pop the OLDEST queued type-ahead line and hand
    it to `start` — the SAME path a typed line takes, so a persistent
    marathon receives it as a `new_task` into the running session. ONE item
    per completion; the rest stay queued FIFO (each finished turn drains the
    next). `mark` (sink.queued_run) announces the handoff — `▶ running queued
    message` — right before the drained line's normal ❯ user-echo, so a drain
    is never a silent start. The popped line is ALREADY OUT of `pending` the
    moment it's read — a `mark` error must never lose it (swallowed: it's
    only a render-side announcement) and a `start` error must put it BACK at
    the head before propagating (a broken start must not silently vanish a
    queued line). Returns True when a drain happened."""
    if not pending:
        return False
    text = pending.popleft()
    if mark is not None:
        try:
            mark(len(pending))
        except Exception:
            pass          # a render error must never lose the popped line
    try:
        start(text)
    except Exception:
        pending.appendleft(text)
        raise
    return True


def _is_queue_command(text: str) -> bool:
    """PURE. `/queue` and its subcommands MANAGE the type-ahead queue, so they
    must run exactly when the queue matters — mid-turn. The Enter handler
    routes them past the busy gate (they never type-ahead-queue themselves)."""
    parts = (text or "").strip().lower().split()
    return bool(parts) and parts[0] == "/queue"


def _arrow_up_action(event, buf, sel: dict, n: int, busy: bool, pending=None,
                     pulled=None) -> None:
    """Up key — precedence: (1) QUEUE-PULL: pending items + an EMPTY buffer
    (busy or idle — NOT busy-gated: the queue legally survives a user STOP
    into idle, and pulling the newest to edit is exactly what you want after
    an Esc) pull the NEWEST queued line out of the queue into the input for
    editing; re-submit re-queues it at the tail (busy) or runs it (idle).
    Repeated presses walk newest→oldest, one item per press; a buffer with
    ANY text is never clobbered (history/step-nav serve it instead). When
    `pulled` (run_session's one-shot carry dict) is given, the pulled item's
    text + steer_iid are recorded into it so _rewrap_pulled can hand the SAME
    iid back if the line is resubmitted unedited (default None keeps the old
    behavior for direct-call tests that don't care).
    (2) Step-navigation EXACTLY as before (steps + empty input + idle —
    reachable exactly when the queue is empty, i.e. today's behavior verbatim
    in the queue-empty world). (3) Recall an older submitted line
    (readline-style)."""
    if pending and not buf.text:
        item = pull_item(pending, buf, len(pending) - 1)   # newest — "edit the last thing I queued"
        if item is not None and pulled is not None:
            pulled["text"], pulled["iid"] = str(item), getattr(item, "iid", "")
        event.app.invalidate()
        return
    if n and not buf.text and not busy:
        sel["i"] = (n - 1) if sel["i"] is None else max(0, sel["i"] - 1)
        event.app.invalidate()
        return
    buf.history_backward()
    event.app.invalidate()


def _arrow_down_action(event, buf, sel: dict, n: int, busy: bool) -> None:
    """Down key — mirror of _arrow_up_action: step-nav on the same exact
    gate, otherwise cycle history forward."""
    if n and not buf.text and not busy:
        sel["i"] = 0 if sel["i"] is None else min(n - 1, sel["i"] + 1)
        event.app.invalidate()
        return
    buf.history_forward()
    event.app.invalidate()


def _rewrap_pulled(pulled: dict, text: str):
    """PURE. A pulled queued line resubmitted UNCHANGED keeps its steer_iid so
    the kernel ring can still dedup a landed twin (W1 front-3b: pull-to-edit
    previously minted a fresh iid — a genuine duplicate turn when the original
    inject had landed). ANY edit = a genuinely new message = fresh iid (the
    landed twin said something else). One-shot: `pulled` is consumed (reset
    to empty) on every call, whether or not it held anything, so a later
    unrelated Enter never sees a stale carry."""
    iid, orig = pulled.get("iid", ""), pulled.get("text", "")
    pulled["iid"] = ""
    pulled["text"] = ""
    if iid and text == orig:
        return QueuedLine(text, iid)
    return text


def _swap_history(buf, slot) -> None:
    """Per-slot input-history plumbing (W4a Task 3 — Task 5 wires it to an
    actual tab switch): the dock keeps ONE shared Buffer (one input box,
    unchanged), but each slot should recall (↑ readline-style) only the
    lines IT submitted — so a slot's OWN `InMemoryHistory` is minted on
    first touch (stored on `slot.history`) and the shared buffer is
    re-pointed at it. A repeat call for the SAME slot reuses its existing
    history verbatim (never mints a second one, never loses recall)."""
    from prompt_toolkit.history import InMemoryHistory
    if slot.history is None:
        slot.history = InMemoryHistory()
    buf.history = slot.history


def _restore_draft(buf, slot) -> None:
    """0.3.24 (per-tab drafts — the browser-tab model: each tab keeps its own
    form state): `buf.reset()` must run FIRST (it clears the history-load
    task state -- unchanged from before this fix), THEN the buffer's live
    text/cursor are set from `slot`'s OWN stashed draft -- restoring exactly
    what was mid-type in THIS tab the last time it was left, never another
    tab's. `min(...)` guards a shrunk/replaced draft never leaving the
    cursor past the end of the text it's now landing on."""
    buf.reset()
    buf.text = slot.draft
    buf.cursor_position = min(slot.draft_cursor, len(buf.text))


async def run_session(*, slots, on_line, on_cycle, on_tier_cycle=None, steps_nav=None,
                      stop_turn=None, queued_run=None, inject=None,
                      home_input=None, cancel_slot=None, ui_hooks=None,
                      on_switch=None, on_new=None, on_paste=None,
                      live=None, update_state=None) -> bool:
    """The full-screen dock: EVERYTHING visible resolves `slots.active()` AT
    CALL TIME (W4a Task 3 — the single most structural change of the
    multisession-tabs wave: no more one session's objects captured once at
    the top). `slots.active().pane` fills the top (a `DynamicContainer`, so
    it re-resolves on every redraw — a tab switch repaints a different
    slot's transcript with zero stale references); a bordered input box +
    toolbar are FIXED at the bottom, shared by every tab (one Buffer — see
    `_swap_history`, wired in a later task). Enter either resolves a pending
    consent reply on the ACTIVE slot's sink (ICNLI: raw verbatim) or starts a
    turn as a BACKGROUND task PINNED to the slot it started in
    (`_start_turn_in` captures that slot ONCE — its own turn dict, queue
    drain, and turn-failed read all stay targeted at it even if the user
    switches tabs while it runs; every OTHER read in this function — Esc/
    Ctrl-C, the toolbar, the queue/todo panels, ↑/click-pull — always acts on
    whatever slot is VISIBLE right now). While a turn runs, Enter on that
    SAME slot queues the line (type-ahead — the LIVE queue panel above the
    input shows it and the toolbar shows `⋯N queued` in accent) or flies it
    into the running turn via `inject` (mid-turn inject, 0.3.15) — both keyed
    off the active slot's own `pending`/`turn`/`pulled`/`qp_ui`/`tp_ui`
    (`SessionSlot` fields, not this function's own locals anymore). A Home
    slot (`sink=None`) has no busy/consent/queue/todo state at all — the
    `_sink_attr` accessor's `default` covers every read; Enter with a
    non-command line on Home calls `home_input(text)` when the caller wired
    one (Task 6 — `None` means ignored), while a slash command on Home still
    reaches `on_line`, same as every other slot. `steps_nav`/`stop_turn`/
    `inject`/`on_cycle`/`queued_run` are INJECTED callables the repl already
    resolves through `slots.active()` itself before handing them here — this
    function only calls them, it never reaches into a session object through
    them. A tab bar (Task 4, `webbee.tabs.tab_fragments`) is pinned at the
    very top of the dock, ALWAYS visible: a click switches tabs (`_switch_to`
    — a no-op on the already-active tab or a stale idx, since
    `slots.switch` already guards both); a session tab's ✕ closes THAT tab
    (`_close_tab_click(idx)` -> `webbee.slots.close_at(slots, idx,
    cancel_slot)`, Task 7 -- the clicked idx, not necessarily whichever tab
    is active), while Ctrl-W AND Ctrl-D-with-other-tabs-open have no per-tab
    idx of their own and keep reaching `_close_flow` -> `close_active(slots,
    cancel_slot)` (Task 5) — always "close what I'm looking at". Both PT-free
    functions live in `webbee.slots` (`close_active` is a thin wrapper over
    `close_at`), shared verbatim with repl's `/close` command. `cancel_slot`
    (a NEW repl-injected callable) tears down the removed slot's OWN
    background tasks; the kernel's run keeps going server-side regardless
    (browser-tab model). `Ctrl-T` and `Alt+0..9` (prompt_toolkit sees the
    latter as the two-key sequence `("escape", "<digit>")`) both land on
    `_switch_to` — the bare `escape` binding (stop-turn / step-clear) stays
    registered too; prompt_toolkit's own key-processor timeout disambiguates
    a lone Escape from an Escape-then-digit chord, same mechanism its own
    default emacs bindings already lean on for `escape,f`/`escape,b`/etc —
    `app.timeoutlen` is turned down well below its 1.0s default (see below)
    so a genuine lone Escape still resolves quickly instead of only your
    patience finding out. `ui_hooks` (optional, repl-owned mutable dict):
    this function fills `ui_hooks["switch"] = _switch_to` and
    `ui_hooks["close"] = _close_flow` at construction time, so repl's
    `/tab`/`/new`/`/close` commands route through the EXACT same switch/close
    path the keys and clicks use (the history swap on every switch, the
    close note) instead of mutating
    `slots` directly and missing it. `on_switch` (Task 6, optional): called
    with the NEW active idx after every successful `_switch_to` -- click,
    Ctrl-T, Alt+N, or a repl command via `ui_hooks["switch"]` all converge
    on `_switch_to`, so this one seam covers every path. repl wires it to
    its own stale-Home-refill check (`home.is_stale` + `fill_home`
    re-scheduled as a bg task) -- this function has no idea what "Home" or
    "stale" mean, it only calls the hook. `on_new` (0.3.25, optional): the
    tab bar's trailing + chip fires this — a bare async callable, no args,
    fired as a background task (`_new_tab_click`, same "can't await from a
    mouse handler" shape as `_launch_inject`) — repl wires it to the EXACT
    same flow `/new` (no arg) uses, which itself calls `ui_hooks["switch"]`
    to land on the new tab, so the history/draft swap always runs through
    `_switch_to` too, never bypassed. `None` (the default, and every test
    that doesn't care) makes a + click a harmless no-op via `tabs.
    tab_fragments`'s own contract.

    Busy-close confirm (Part D): a ✕ click on a tab whose OWN turn task is
    still alive (`slots.is_turn_alive`) arms `slot.close_armed` instead of
    closing outright — `_close_tab_click` below — and a note lands in that
    tab's own transcript; the tab bar then renders "✕?" (`tabs.
    tab_fragments`) until either a second click on the SAME armed tab
    actually closes it, or ANY switch/keypress disarms it again
    (`slots.disarm_all`, wired into `_switch_to` and the Application's
    `after_key_press` event below).
    Returns True on clean exit; False if prompt_toolkit is unavailable
    (the caller uses the plain fallback loop)."""
    try:
        from prompt_toolkit.application import Application, get_app, get_app_or_none
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer,
                                          HSplit, Layout, Window)
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except Exception:
        return False

    def _a():
        return slots.active()

    def _pane():
        p = _a().pane
        # Middle-click paste (webbee-code-mouse-middle-click-paste-v1):
        # wire the hook lazily + idempotently on first touch of each pane --
        # avoids threading a new constructor arg through OutputPane's THREE
        # call sites (repl.py x2, home_view.py) for one optional callback.
        if getattr(p, "on_middle_paste", None) is None:
            p.on_middle_paste = _middle_paste
        if getattr(p, "on_right_paste", None) is None:
            p.on_right_paste = _right_paste
        if getattr(p, "on_line_click", None) is None:
            p.on_line_click = _line_click
        return p

    def _sink_attr(name, default=None):
        s = _a().sink
        return getattr(s, name, default) if s is not None else default

    buf = Buffer(multiline=False)

    def _launch_inject(text):
        # Fire the fly-in as a background task (a key handler can't await):
        # _inject_or_queue owns the iid mint + the local-queue fallback.
        # `slot` is captured HERE, SYNCHRONOUSLY, at Enter keypress time
        # (FIX7a — mirrors the turn-start pinning everywhere else in this
        # module) -- both the target deque AND the inject POST itself stay
        # pinned to THIS slot even if the user switches tabs before the
        # scheduled background task's body actually runs. `inject` itself
        # keeps its existing 2-arg (text, iid) contract as far as
        # `_inject_or_queue` is concerned -- the slot rides along in a thin
        # wrapper closure, so `_inject_or_queue`'s own signature/tests need
        # no change at all.
        slot = _a()
        app = get_app()
        app.create_background_task(
            _inject_or_queue(lambda t, i: inject(t, i, slot), text, slot.pending,
                             invalidate=app.invalidate))

    def _start_turn_in(slot, text):
        slot.turn.pop("stopped", None)   # a stale stop flag must never eat the next natural drain
        slot.turn["task"] = get_app().create_background_task(_run_turn(slot, text))

    def _start_turn(text):
        _start_turn_in(_a(), text)

    def _finish_natural_turn(slot, done: bool) -> None:
        """Shared finally-tail of ONE turn (typed via `_run_turn` below, OR
        an attach pickup via `_start_attach_in`'s wrapper) -- clears
        `slot.turn["task"]`, then the DRAIN RULE: natural completion ONLY.
        A user STOP (Esc/Ctrl-C sets turn["stopped"] before cancelling; the
        caller absorbs the cancel so `done` stays False) means "I'm taking
        control" — the queue is PRESERVED, stays visible (toolbar accent +
        /queue), and never auto-runs; /queue clear drops it, and the next
        NATURAL completion resumes draining. A propagating exception (dock
        teardown) also leaves the queue be. An ERROR-terminated turn
        (slot.sink.status()["turn_failed"], set by repl's except branch via
        RichSink.mark_turn_failed — W1 front-3b) holds the queue too: a
        broken backend must never burn one queued line per failing turn.
        Factored out of `_run_turn` so an attach pickup gets the EXACT SAME
        queue-drain/stop/failed semantics a typed turn has, instead of a
        parallel half-copy that silently drifts."""
        slot.turn["task"] = None
        stopped = slot.turn.pop("stopped", False)
        failed = False
        try:
            failed = bool(slot.sink.status().get("turn_failed"))
        except Exception:
            pass
        if done and not stopped and not failed:
            # Submit the oldest queued line through the SAME path a typed
            # line takes, back into the SAME (pinned) slot; queued_run
            # announces it — never a silent start.
            _drain_pending(slot.pending, lambda t: _start_turn_in(slot, t), mark=queued_run)
        get_app().invalidate()

    def _start_attach_in(slot, coro):
        """Attach-on-poll's own start-turn seam (`ui_hooks["start_attach_in"]`,
        registered below) -- mirrors `_start_turn_in`'s task-tracking so Esc/
        Ctrl-C can cancel an in-flight attach turn exactly like a typed one
        (both key handlers cancel `slot.turn.get("task")` directly), and
        `_finish_natural_turn` above so its queue-drain/stop/failed tail is
        identical too. There is no `on_line`/text path for an attach (the
        caller -- repl's `poll_idle_steer` `attach_turn` seam -- already
        built the whole turn's coroutine, `sink.begin_turn()` through
        `end_turn()`), so this takes a ready-made coroutine rather than
        routing through `_run_turn`; it returns the spawned task so the
        caller can await it itself (the poller must block for the turn's
        whole duration -- unlike a typed Enter, which is fire-and-forget
        from a key handler)."""
        async def _wrapped():
            done = False
            try:
                await coro
                done = True
            finally:
                _finish_natural_turn(slot, done)
        slot.turn.pop("stopped", None)
        slot.turn["task"] = get_app().create_background_task(_wrapped())
        return slot.turn["task"]

    async def _run_turn(slot, text):
        # `slot` is bound ONCE by the caller (Enter's idle path, or a drain
        # re-submitting into the SAME slot it drained from) -- a turn belongs
        # to the slot it started in. Every read/mutation below stays pinned
        # to THAT slot even if the user switches tabs while it runs; this is
        # the one deliberate exception to "always resolve active() at call
        # time" everywhere else in this function.
        done = False
        try:
            await on_line(text, slot)
            done = True
        finally:
            _finish_natural_turn(slot, done)

    # Shift+Enter (CSI-u) must be known to the parser BEFORE any key is read,
    # so the ("escape","enter") binding below can match it. Best effort: a
    # False return just means Alt+Enter / Ctrl+J remain the newline chords.
    enable_csi_u_newline()

    kb = KeyBindings()

    def _busy_live() -> bool:
        """Lockout-proof busy for the key handlers, on the ACTIVE slot: busy
        only while ITS turn TASK is genuinely alive. A turn that died without
        clearing the sink's busy flag (an error path that skipped end_turn)
        must never brick the dock -- Enter/Esc/Ctrl-C all gate on THIS, so a
        stale flag degrades to a cosmetic toolbar glitch instead of an
        unusable input (Valentin, live 2026-07-15: 'working' spun and NO key
        reacted). A Home slot (no sink) is never busy. Thin wrapper over the
        per-slot `_slot_busy` (FIX5) -- Ctrl-D's `_eof` uses THAT directly,
        across every slot, not just the active one."""
        return _slot_busy(_a())

    def _slot_busy(slot) -> bool:
        """Lockout-proof busy for an ARBITRARY slot (FIX5, generalizes
        `_busy_live` above -- same predicate, parameterized): busy only
        while ITS OWN turn TASK is genuinely alive AND its sink reports
        busy. Ctrl-D's `_eof` needs this across EVERY slot, not just the
        active one -- a background tab's live turn must never let a Ctrl-D
        pressed on Home (or any other idle tab) exit right through it."""
        t = slot.turn.get("task")
        sink = slot.sink
        ib = getattr(sink, "is_busy", None) if sink is not None else None
        busy = bool(ib()) if callable(ib) else False
        return bool(busy and t is not None and not t.done())

    @kb.add("enter")
    def _enter(event):
        # never send leaked mouse reports; _rewrap_pulled keeps the ORIGINAL
        # steer_iid alive when this is a pulled queued line resubmitted
        # UNCHANGED (one-shot — the ACTIVE slot's `pulled` is consumed
        # either way).
        slot = _a()
        text = _rewrap_pulled(slot.pulled, scrub_mouse_residue(buf.text))
        buf.reset()
        # 0.3.24: a genuine Enter retires THIS slot's own stashed draft too
        # -- otherwise the NEXT switch-away-then-back (nothing typed in
        # between) would restore text that was already sent, resurrecting a
        # message the user believes is long gone.
        slot.draft = ""
        slot.draft_cursor = 0
        if slot.kind == "home" and not text.strip():
            # Empty Enter on the interactive Home activates the focused item.
            slot.pane.activate_focused()
            return
        cp = _sink_attr("consent_pending")
        if cp and cp():
            rc = _sink_attr("resolve_consent")
            if rc:
                rc(text)                       # ICNLI: relay the raw reply verbatim
            return
        if not text.strip() and sel["i"] is not None and steps_nav and not _busy_live():
            idx, sel["i"] = sel["i"], None
            event.app.create_background_task(steps_nav["expand"](idx, slot))
            return
        if not text.strip():
            return
        if _is_queue_command(text):
            # Queue MANAGEMENT runs NOW, even mid-turn (it never queues
            # itself): a display-only background task — the handler only
            # reads/clears the shared deque and prints, so it can't collide
            # with the live turn and never touches turn["task"]. `slot` is
            # the ACTIVE-AT-KEYPRESS slot captured above (FIX1) — the SAME
            # slot on_line acts against no matter what becomes active
            # before this scheduled task's body actually runs.
            buf.history.append_string(text)
            event.app.create_background_task(on_line(text, slot))
            return
        if slot.sink is None:
            # Home: no busy/queue/inject semantics at all. A slash command
            # still reaches on_line exactly like every other slot (against
            # the Home slot itself, FIX1); plain text is the caller's own
            # affair (Task 6 wires home_input -- None here simply means
            # "ignored").
            buf.history.append_string(text)
            if text.strip().startswith("/"):
                event.app.create_background_task(on_line(text, slot))
            elif home_input is not None:
                event.app.create_background_task(home_input(text))
            return
        # Non-empty line: record for up-arrow recall, then fly-while-busy
        # (mid-turn inject — the line reaches the RUNNING turn within one
        # brain step; the kernel's task_queued[terminal] echo shows the panel
        # row) with local type-ahead as the no-inject/failure fallback, or
        # start a turn now (idle — unchanged).
        if _submit_line(text, buf, slot.pending, _busy_live(), _start_turn,
                        inject=None if inject is None else _launch_inject
                        ) in ("queued", "injected"):
            event.app.invalidate()             # panel + toolbar show the new depth

    # ---- newline INSIDE the prompt (0.3.36) -------------------------------
    # Enter still SENDS (muscle memory is sacred); these insert a literal
    # newline so one prompt can be many lines. THREE chords, because no
    # single one is portable across every terminal/OS/SSH client:
    #   * Alt+Enter   -> ("escape", "enter"). Universal: every terminal sends
    #     ESC as the Alt prefix. Same two-key shape as the Alt+N tab
    #     switches already bound above.
    #   * Shift+Enter -> ALSO ("escape", "enter"), because enable_csi_u_newline()
    #     registers the CSI-u codes modern terminals emit for it. This is the
    #     chord most people try first, so it works wherever the terminal can
    #     express it -- and degrades to Alt+Enter/Ctrl+J where it cannot.
    #   * Ctrl+J      -> a raw LF (0x0a) that EVERY terminal, tmux/screen and
    #     ssh client can send, on every OS: the guaranteed fallback.
    # Buffer(multiline=False) stores embedded "\n" fine and input_rows()
    # already counts them, so the input box grows by itself.
    def _insert_newline(event):
        event.current_buffer.insert_text("\n")

    kb.add("escape", "enter")(_insert_newline)   # Alt+Enter (+ Shift+Enter via CSI-u)
    kb.add("c-j")(_insert_newline)               # raw LF -- universal fallback

    @kb.add("s-tab")
    def _cycle(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.focus_prev()
            event.app.invalidate()
            return
        on_cycle()
        event.app.invalidate()

    @kb.add("c-b", filter=Condition(lambda: on_tier_cycle is not None and _a().kind != "home"))
    def _tier_cycle(event):
        # webbee-code-model-tier-slash-command-v1: Ctrl+B cycles the coding
        # brain tier (webbeesmart -> supersmart -> ultrasmart -> webbeesmart), the exact
        # keyboard-symmetric sibling of Shift+TAB's mode cycle above. NOT
        # Ctrl+M: that's the same raw byte (\r, CR) as Enter on most
        # terminals lacking CSI-u/Kitty-protocol support -- binding it would
        # silently break message-send on the majority of real terminals.
        # Gated off Home (no repo, nothing to persist a tier choice against)
        # exactly like /model itself is gated (_HOME_GATED_ACTIONS).
        on_tier_cycle()
        event.app.invalidate()

    @kb.add("c-c")
    def _interrupt(event):
        _interrupt_action(_a().turn, _busy_live, stop_turn, event)

    @kb.add("c-t")
    def _new_tab_key(event):
        # 0.3.26: Ctrl-T opens a NEW tab (the browser gesture), via the exact
        # seam the tab bar's + chip uses (`_new_tab_click` -> on_new ->
        # repl._open_new_tab). Home stays reachable by clicking its ◆ chip or
        # Alt+1-style switch (footer legend reminds muscle-memory users).
        _new_tab_click()

    def _middle_paste() -> None:
        # webbee-code-mouse-middle-click-paste-v1: X11/Linux middle-click ->
        # paste PRIMARY selection (falls back to CLIPBOARD text where PRIMARY
        # doesn't exist -- macOS/Windows have no PRIMARY concept at all, so
        # this degrades to a plain Ctrl+V-style text paste there). A mouse
        # handler is SYNC (selection.py can't await), so this schedules its
        # own background task -- mirrors _paste_key's own worker-thread
        # discipline (shelling out inline would freeze the whole dock).
        from webbee.clipboard_read import read_clipboard_text, read_primary_text
        slot = _a()
        pane = _pane()
        if slot.kind == "home" or not getattr(slot, "workspace", ""):
            pane.flash_note("📎 open a session tab to paste")
            return

        async def _do_middle_paste():
            text = await asyncio.to_thread(read_primary_text)
            if not text:
                text = await asyncio.to_thread(read_clipboard_text)
            if not text:
                pane.flash_note("selection is empty")
                get_app().invalidate()
                return
            buf.insert_text(text)
            get_app().invalidate()

        get_app().create_background_task(_do_middle_paste())

    def _right_paste() -> None:
        # webbee-code-mouse-right-click-paste-v1: the OTHER common terminal
        # paste convention -- right-click reads the regular OS CLIPBOARD
        # (text OR image), the exact same source + upload door as Ctrl+V
        # (_paste_key below), just triggered by a mouse hook instead of a
        # key binding. Kept as its own tiny function (not a call into
        # _paste_key, which needs a real `event` with `.app`) so the two
        # entry points stay independently readable; the guts are a
        # deliberate mirror of _do_paste below -- same clipboard read, same
        # upload door, same flash-note vocabulary.
        import time as _t
        from webbee.clipboard_read import read_clipboard
        slot = _a()
        pane = _pane()
        if on_paste is None or slot.kind == "home" or not getattr(slot, "workspace", ""):
            pane.flash_note("📎 open a session tab to paste a file")
            return
        stamp = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime())

        async def _do_right_paste():
            item = await asyncio.to_thread(read_clipboard, stamp)
            if item is None:
                pane.flash_note("clipboard is empty")
                get_app().invalidate()
                return
            if item.kind == "text":
                buf.insert_text(item.data)
                get_app().invalidate()
                return
            pane.flash_note(f"📎 uploading {item.name}…", secs=30.0)
            get_app().invalidate()
            ref = ""
            try:
                ref = await on_paste(slot.workspace, item.name, item.data)
            except Exception:
                ref = ""
            if ref:
                sep = "" if (not buf.text or buf.text.endswith(" ")) else " "
                buf.insert_text(sep + ref + " ")
                pane.flash_note(f"📎 {item.name}")
            else:
                pane.flash_note("📎 paste failed")
            get_app().invalidate()

        get_app().create_background_task(_do_right_paste())

    def _line_click(abs_line: int) -> None:
        """webbee-code-click-to-expand-v1: a PLAIN click on a transcript line
        (see selection.py's `on_line_click` hook) asks \"what happened here?\"
        without touching the keyboard at all -- the exact same reveal /steps
        N (and Up/Down + Enter) already show, just one gesture shorter. A
        click resolves to a RECORD via the pane's own reflow-anchoring math
        (`_record_at_line`, already used for reflow + scroll-anchoring), then
        to a step number via the sink's `step_for_record` (set right after
        each tool_result print, see render.py) -- so a click on a banner,
        a progress line, or blank space (no step there) is a harmless no-op,
        never an error. Busy mid-turn is ALSO a no-op (steps_nav reflects the
        LAST finished turn only -- clicking during a live one would expand
        stale data), same gate Enter's own steps_nav path already uses.
        Compact by construction: dispatches through steps_nav[\"expand\"],
        the SAME `/steps N` -> step_detail bordered panel Enter already
        shows -- never a new UI surface, never full-screen, nothing to
        keep in sync twice."""
        if not steps_nav or _busy_live():
            return
        slot = _a()
        sink = slot.sink
        step_fn = getattr(sink, "step_for_record", None) if sink is not None else None
        if step_fn is None:
            return
        pane = _pane()
        record_idx = pane._record_at_line(abs_line)
        step_no = step_fn(record_idx)
        if not step_no:
            return
        # webbee-code-step-toggle-v1: a SECOND click on the SAME already-open
        # step line folds the detail panel back away instead of re-printing
        # it -- a plain flash_note toast (same mechanism as copy-confirm)
        # says so out loud, tool-agnostic (works identically whether the
        # step was bash/read_file/write_file/anything else): raising and
        # lowering the detail is now a real, visible round-trip, not a
        # one-way reveal.
        if slot.expanded_steps.get(step_no):
            slot.expanded_steps.pop(step_no, None)
            pane.flash_note(f"▸ step {step_no} collapsed")
            get_app_or_none() and get_app_or_none().invalidate()
            return
        slot.expanded_steps[step_no] = True
        event = get_app_or_none()
        if event is None:
            return
        event.create_background_task(steps_nav["expand"](step_no - 1, slot))

    @kb.add("c-v")
    def _paste_key(event):
        # 0.3.34 (W3 Wave A): Ctrl-V pastes the OS clipboard. An IMAGE is read
        # OUT-OF-BAND (a terminal's bracketed paste is text-only), then saved +
        # uploaded via the file-reader door (repl's on_paste) and its reference
        # dropped into the input; a text clipboard is inserted inline. Normal
        # text paste (Cmd-V / Ctrl-Shift-V → bracketed paste) is a DIFFERENT
        # key and is unaffected. Home has no workspace/agent → guide, not crash.
        import time as _t
        from webbee.clipboard_read import read_clipboard
        slot = _a()
        pane = _pane()
        if on_paste is None or slot.kind == "home" or not getattr(slot, "workspace", ""):
            pane.flash_note("📎 open a session tab to paste a file")
            event.app.invalidate()
            return
        # read_clipboard() shells out to the platform clipboard tool
        # (osascript+pbpaste on macOS, xclip/wl-paste on Linux) with a 2s
        # timeout EACH. Measured on this host: 83.7ms for a plain-text
        # clipboard (73.5 osascript + 10.1 pbpaste), and up to ~4s if both
        # timeouts are hit. Running that inline froze the whole dock -- no
        # repaint, no spinner, no keystrokes -- for the duration, because a
        # key binding runs ON the event loop. So it moves to a worker thread;
        # `slot`/`pane` are already captured synchronously above (same
        # discipline as _run_turn), so the paste lands on the tab that was
        # active when the key was pressed even if the user switches tabs
        # mid-read.
        stamp = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime())

        async def _do_paste():
            item = await asyncio.to_thread(read_clipboard, stamp)
            if item is None:
                pane.flash_note("clipboard is empty")
                event.app.invalidate()
                return
            if item.kind == "text":
                buf.insert_text(item.data)
                event.app.invalidate()
                return
            pane.flash_note(f"📎 uploading {item.name}…", secs=30.0)
            event.app.invalidate()
            ref = ""
            try:
                ref = await on_paste(slot.workspace, item.name, item.data)
            except Exception:
                ref = ""
            if ref:
                sep = "" if (not buf.text or buf.text.endswith(" ")) else " "
                buf.insert_text(sep + ref + " ")
                pane.flash_note(f"📎 {item.name}")
            else:
                pane.flash_note("📎 paste failed")
            event.app.invalidate()

        event.app.create_background_task(_do_paste())

    def _alt_digit_handler(d: int):
        def _h(event):
            _switch_to(d)
        return _h

    for _d in range(10):
        # Alt+N == prompt_toolkit's two-key sequence ("escape", "<digit>") —
        # the SAME mechanism its own default emacs bindings use for
        # escape+f/escape+b/etc, coexisting with the plain "escape" binding
        # below (stop-turn / step-clear) via the key-processor's own
        # prefix-of-longer-match timeout, tuned down further below.
        kb.add("escape", str(_d))(_alt_digit_handler(_d))

    # webbee-code-linux-tab-switch-fallback-v1 (Valentin, live, PopOS: "на
    # маке Option+tab number работает идеально, а на линуксе вообще нет"):
    # this is NOT a webbee bug -- most Linux terminal emulators (GNOME
    # Terminal, Terminator, and other VTE-based ones) bind Alt+1..9
    # THEMSELVES as their own "jump to terminal tab N" accelerator, so the
    # chord never reaches webbee's input stream there at all; macOS's
    # Terminal.app/iTerm2 ship with no such default, which is exactly why
    # the same chord "just works" there. Ctrl+<digit> was tried and reverted
    # -- most digits have NO classic control byte at all without a
    # modifyOtherKeys/CSI-u terminal mode GNOME Terminal doesn't send by
    # default, so it would silently never fire on the very box that needs
    # it. F1..F9 instead: real, universally-supported escape sequences
    # every terminal emulator (VTE included) has forwarded untouched since
    # the 80s, essentially never claimed by the terminal itself for tab
    # switching.
    for _d in range(1, 10):
        kb.add(f"f{_d}")(_alt_digit_handler(_d))

    @kb.add("c-w", filter=Condition(lambda: _can_close_tab(buf, _a())))
    def _close_tab_key(event):
        # Filtered, not unconditional (contract): an empty input on an
        # active SESSION tab closes it; any OTHER state (draft text present,
        # or Home active) leaves this binding's filter False, so the match
        # falls through to prompt_toolkit's own default emacs/basic Ctrl-W
        # (unix-word-rubout) untouched.
        _close_flow()

    @kb.add("c-d")
    def _eof(event):
        # Ctrl-D policy (Task 5, generalized FIX5): closing the active
        # SESSION tab is the natural action as long as another session tab
        # survives it; otherwise a running turn must never be torn down by
        # a stray EOF -- but FIX5 widens that guard past the ACTIVE slot:
        # a background tab's live turn (e.g. Home-spawned via _home_input,
        # or any tab left running while you switched away) must ALSO block
        # exit, not just whichever slot happens to be visible right now.
        if _should_close_on_eof(slots):
            _close_flow()
            return
        if any(_slot_busy(s) for s in slots.slots):
            return
        event.app.exit()

    sel = {"i": None}   # None = no selection; else 0-based step index

    def _nav_count() -> int:
        try:
            return int(steps_nav["count"]()) if steps_nav else 0
        except Exception:
            return 0

    @kb.add("up")
    def _step_up(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.move_focus(-1)
            return
        _arrow_up_action(event, buf, sel, _nav_count(), _busy_live(), slot.pending, slot.pulled)

    @kb.add("down")
    def _step_down(event):
        slot = _a()
        if slot.kind == "home" and not buf.text:
            slot.pane.move_focus(1)
            return
        _arrow_down_action(event, buf, sel, _nav_count(), _busy_live())

    @kb.add("escape")
    def _step_clear(event):
        _escape_action(sel, _a().turn, _busy_live, stop_turn, event, buf=buf, sink=getattr(_a(), "sink", None))

    _home_nav = Condition(lambda: _a().kind == "home" and not buf.text)

    @kb.add("tab", filter=_home_nav)
    def _home_focus_next(event):
        _a().pane.focus_next()

    @kb.add("left", filter=_home_nav)
    def _home_seg_left(event):
        _a().pane.seg_left()

    @kb.add("right", filter=_home_nav)
    def _home_seg_right(event):
        _a().pane.seg_right()

    @kb.add("pageup")
    def _pgup(event):
        pane = _pane()
        pane.scroll(-(max(1, pane._view_h) - 2))

    @kb.add("pagedown")
    def _pgdn(event):
        pane = _pane()
        pane.scroll(max(1, pane._view_h) - 2)

    def _badge_style_override() -> str:
        """webbee-code-badge-calm-v1: calm, steady version badge WITHOUT blinking
        or flashing. Returns consistent class:tb.fresh when confirmed fresh so
        there is zero annoying flicker."""
        us = update_state or {}
        if us.get("checked") is not True or (us.get("notice") or "").strip():
            return ""
        return "class:tb.fresh"

    def _badge_cycle_text() -> str:
        """webbee-code-badge-cycle-v1: in the ONE honest "confirmed fresh"
        state (checked True, no update notice), alternate the badge's TEXT
        every ~5s between the bare version number and the words "up to
        date" -- two short, distinct beats, never both on one line -- so the
        freshness display visibly LIVES instead of sitting there as one
        static string forever (Valentin, live 2026-07-31: wants proof this
        is a real, ongoing check, not a cached one-liner printed once).
        Driven off wall-clock time, same source as `_badge_style_override`'s
        colour pulse -- no extra timer needed, the idle 1.0s tick already
        redraws often enough to catch each 5s flip. Returns "" (no override,
        `pin_version_right` falls back to its own text) for every other
        state -- offline/unchecked/update-available keep their normal,
        single, non-cycling text."""
        us = update_state or {}
        if us.get("checked") is not True or (us.get("notice") or "").strip():
            return ""
        import time as _t
        phase = int(_t.monotonic() // 5) % 2
        return f"v{__version__}" if phase == 0 else "up to date"

    def _current_badge_text() -> str:
        """The exact text `pin_version_right` will draw right now, so the
        width reservation below always matches (0.3.40: the badge is no
        longer a fixed-length " v<version> " — an "update available" state
        is longer, and must reserve accordingly, or it would get dropped for
        not fitting a reservation sized for the SHORTER plain form).
        0.3.48: the confirmed-fresh state now CYCLES between two different-
        length texts ("v0.3.47" vs "up to date") -- reserving for whichever
        one happens to be showing this exact frame would make the hint text
        next to it grow/shrink every 5s, a visible jank. Reserve the WIDER
        of the two variants ALWAYS, regardless of which phase is live --
        the shorter phase just leaves a little slack before the badge,
        never a truncation."""
        us = update_state or {}
        checked = us.get("checked")
        if checked is None:
            return version_badge_text(__version__)
        if checked is True and not (us.get("notice") or "").strip():
            widest = max(f"v{__version__}", "up to date", key=len)
            return f" {widest} "
        from webbee.home_view import version_badge
        text, _cls = version_badge(__version__, us.get("notice", ""), checked=checked)
        return f" {text} "

    def _toolbar_fit_width() -> int:
        """Columns available to the toolbar's own text: the true terminal width
        MINUS the space the bottom-right version badge will occupy (0.3.37).

        Without this reservation the hint text would fit itself to the FULL
        width and `pin_version_right` would then find no room left, so the
        badge would blink out exactly on the narrow terminals where a fixed
        anchor matters most. Reserving first makes the badge deterministic:
        the hints degrade (they already do, by design), the badge stays put.
        0 (unknown/headless) is passed through untouched."""
        w = sizing.get_size(get_app_or_none())[0]
        if not w or w <= 0:
            return 0
        return max(1, w - len(_current_badge_text()))

    def _toolbar():
        pane = _pane()
        f = pane.flash()
        if f:
            frags = [("class:tb.working", "  " + f)]   # transient copy confirmation
        elif sel["i"] is not None and steps_nav:
            frags = [("class:tb.dim", f"  step {sel['i'] + 1}/{_nav_count()} · Enter to expand · Esc to cancel")]
        else:
            st_fn = _sink_attr("status")
            if callable(st_fn):
                slot = _a()
                st = st_fn()
                frags = build_toolbar(slot.mode, st["tokens"], st["credits"], busy=st["busy"],
                                      current=st["current"], elapsed=st["elapsed"],
                                      tools=st["tools"], consent=st["consent"],
                                      queued=len(slot.pending) + len(_sink_attr("remote_pending", [])),
                                      reconnecting=st.get("reconnecting", 0),
                                      width=_toolbar_fit_width(),
                                      live=str((live or {}).get("text") or ""),
                                      tier=getattr(slot, "model_tier", "") or "")
            else:
                # Home has no sink of its OWN, but it is the dock's summary
                # page: show the SESSION TOTAL across every open tab (0.3.36)
                # so spend is visible from every tab, Home included -- the
                # per-tab numbers live in each sink, Home adds them up.
                tk, cr = session_totals(slots)
                frags = [("class:tb.dim", "  ◆ home"),
                         ("class:tb.dim", f"   ·   {_fmt_tokens(tk)} tok · {_fmt_tokens(cr)} credits this session")]
                _w = _toolbar_fit_width()
                _hint = "   ·   type a task to start · Alt/F-key+№ switch"
                if not _w or sum(len(t) for _, t in frags) + len(_hint) <= _w:
                    frags.append(("class:tb.dim", _hint))
        # W2 Task 8: the toolbar has no mouse handling of its own, so
        # `_forwarding(None, pane)` is wrapped onto every fragment purely for
        # drag-forwarding — a release that lands on the toolbar row while a
        # pane selection is armed still completes the copy instead of
        # sticking. `build_toolbar` itself stays untouched/2-tuple (its own
        # unit tests unpack `for _, seg in frags`).
        # 0.3.37: the version badge is pinned to the row's right edge HERE --
        # one call, AFTER every state branch above has built its fragments, so
        # idle/busy/consent/reconnecting/copy-flash/step-nav/Home all show it
        # in the same fixed spot: the bottom-right corner of the window.
        # 0.3.40: real, live-checked (not just displayed) + INTERACTIVE — a
        # click on the badge flashes the upgrade command into the toolbar
        # (via the pane's own copy-flash mechanism, reusing the exact same
        # visual language a successful copy already uses) instead of the
        # user having to go read the CHANGELOG to find it.
        us = update_state or {}
        pre_len = len(frags)
        frags = pin_version_right(frags, __version__,
                                  sizing.get_size(get_app_or_none())[0],
                                  notice=us.get("notice", ""),
                                  checked=us.get("checked"),
                                  style_override=_badge_style_override(),
                                  text_override=_badge_cycle_text())
        fwd = _forwarding(None, pane)
        badge_appended = len(frags) > pre_len   # pin_version_right may drop it (no room)
        out = []
        for i, (style, text) in enumerate(frags):
            is_badge = badge_appended and i == len(frags) - 1
            if is_badge and us.get("notice"):
                out.append((style, text, _badge_click(pane, us["notice"], fwd)))
            else:
                out.append((style, text, fwd))
        return out

    # Dynamic height: EXACTLY the rows the wrapped input needs (1→cap), so the
    # box grows as you type and shrinks back — never a fixed huge block. Enter
    # still submits (multiline=False); the pane above absorbs all remaining
    # space. Live size (W2 front-2, proportions not pixels): the wrap width
    # comes from the input window's OWN render_info once it has rendered at
    # least once (the true columns inside the frame, after the "❯ " prompt
    # and any margin) — `cols - 4` is the pre-first-render/headless fallback
    # the old shutil-based estimate used; the cap is a PROPORTION of the
    # live rows (sizing.input_height_cap), not a fixed 10.
    def _input_height():
        cols, rows = sizing.get_size(get_app_or_none())
        ri = getattr(input_win, "render_info", None)
        width = getattr(ri, "window_width", None) if ri is not None else None
        return input_rows(buf.text, width or (cols - 4), sizing.input_height_cap(rows))

    def _prompt_fragments():
        # The ❯ takes the CURRENT mode's colour (same classes the toolbar uses)
        # so the mode is obvious from the input line itself, not just the toolbar.
        return [(f"class:tb.mode.{_a().mode}", "❯ ")]

    def _pull_at(index: int) -> None:
        """Mouse pull (a panel row's MOUSE_UP handler, queue_panel._item_handler):
        move the CLICKED queued item into the input for editing — the SAME
        pull_item the ↑ key uses, arbitrary index instead of newest (never
        clobbers a typed draft, ignores a stale index). Mirrors _arrow_up_action:
        records the item's text + steer_iid into the ACTIVE slot's `pulled`
        so an unchanged resubmit keeps the original iid (see _rewrap_pulled)."""
        slot = _a()
        item = pull_item(slot.pending, buf, index)
        if item is not None:
            slot.pulled["text"], slot.pulled["iid"] = str(item), getattr(item, "iid", "")
            get_app().invalidate()

    def _drop_at(index: int) -> None:
        """Mouse remove (a row's ✕ handler, queue_panel._drop_handler): delete
        the CLICKED queued item outright (0.3.37). Unlike `_pull_at` the input
        buffer is untouched — removing the 1st of 3 queued lines must not
        hijack whatever you are currently typing. A note lands in the
        transcript so the removal is visible in scrollback, not just as a row
        that silently vanished."""
        slot = _a()
        item = drop_item(slot.pending, index)
        if item is not None:
            note = getattr(slot.sink, "note", None)
            if note is not None:
                note(f"removed from queue: {one_line(str(item), 60)}")
            get_app().invalidate()

    def _panel_size(floor: int):
        """(cols, item-row cap) shared by a panel's fragment builder AND its
        ConditionalContainer height lambda — ONE size read so the rendered
        rows and the reserved height can never disagree (W2 front-2:
        proportions, not pixels — was the fixed QP/TP_MAX_ITEMS). `floor` is
        each panel's own today's-look constant (queue=5, todo=6) passed
        through to sizing.panel_cap so a normal 24-row terminal keeps its
        pre-W2 row count and only a tall screen grows past it."""
        cols, rows = sizing.get_size(get_app_or_none())
        return cols, sizing.panel_cap(rows, floor)

    def _toggle_queue():
        slot = _a()
        slot.qp_ui["collapsed"] = not slot.qp_ui["collapsed"]
        get_app().invalidate()

    def _toggle_todos():
        slot = _a()
        slot.tp_ui["collapsed"] = not slot.tp_ui["collapsed"]
        get_app().invalidate()

    def _queue_fragments():
        # Live like _toolbar: re-invoked every redraw, reads the ACTIVE
        # slot's own deque + the sink-owned remote list (pull serves the
        # LOCAL rows only — remote rows are display-only by construction in
        # queue_fragments). forward=pane.forward_mouse (W2 Task 8): first
        # refusal on every row/header click so a drag armed on the pane
        # above can still be extended/completed once it releases here.
        slot = _a()
        cols, cap = _panel_size(5)
        return queue_fragments(slot.pending, pull=_pull_at, width=cols,
                               remote=_sink_attr("remote_pending", []),
                               collapsed=slot.qp_ui["collapsed"],
                               toggle=_toggle_queue, max_items=cap,
                               forward=_pane().forward_mouse,
                               drop=_drop_at)

    # The LIVE pending-queue panel — pinned BETWEEN the output pane and the
    # input box; zero rows (hidden) while the queue is empty, so the empty
    # state is pixel-identical to the panel-less dock. focusable=False keeps
    # focus on the input even when a row is clicked.
    queue_panel = ConditionalContainer(
        content=Window(FormattedTextControl(_queue_fragments, focusable=False),
                       height=lambda: queue_height(_a().pending, _sink_attr("remote_pending", []),
                                                   _a().qp_ui["collapsed"],
                                                   max_items=_panel_size(5)[1]),
                       always_hide_cursor=True, wrap_lines=False),
        filter=Condition(lambda: bool(_a().pending) or bool(_sink_attr("remote_pending", []))))

    def _todo_fragments():
        # Live like _queue_fragments: re-invoked every redraw, reads the
        # ACTIVE slot's sink-owned current_todos list in place (todo frames
        # mutate it). forward=pane.forward_mouse (W2 Task 8): same
        # first-refusal seam.
        slot = _a()
        cols, cap = _panel_size(6)
        return todo_fragments(_sink_attr("current_todos", []), width=cols,
                              collapsed=slot.tp_ui["collapsed"],
                              toggle=_toggle_todos, max_items=cap,
                              forward=_pane().forward_mouse)

    # The STICKY todo panel — pinned ABOVE the queue panel (the queue stays
    # adjacent to the input; its bottom row is the ↑-pullable newest). Same
    # proven stacked-ConditionalContainer pattern: zero rows while the list
    # is empty, focusable=False keeps focus on the input.
    todo_panel = ConditionalContainer(
        content=Window(FormattedTextControl(_todo_fragments, focusable=False),
                       height=lambda: todo_height(_sink_attr("current_todos", []),
                                                  _a().tp_ui["collapsed"],
                                                  max_items=_panel_size(6)[1]),
                       always_hide_cursor=True, wrap_lines=False),
        filter=Condition(lambda: bool(_sink_attr("current_todos", []))))

    def _line_prefix(line_no, wrap_count):
        # 0.3.36: with multi-line prompts, line 2+ gets a dim continuation
        # gutter so it reads as ONE message still being composed, never as
        # something already sent. Line 1 keeps the coloured `❯ ` that
        # BeforeInput draws (returning "" leaves it untouched). Aligned to
        # the same 2 columns as `❯ `, so text never shifts as it wraps.
        if line_no == 0 and not wrap_count:
            return ""
        return [("class:input.cont", "\u250a ")]

    _input_control = BufferControl(buffer=buf, input_processors=[BeforeInput(_prompt_fragments)])

    # webbee-code-input-mouse-recursion-fix-v1 (Valentin, live, PopOS crash
    # with "maximum recursion depth exceeded" + 981 repeated frames in
    # _input_mouse_handler): `input_win.content` IS `_input_control` -- the
    # SAME object, not a copy -- so the assignment below used to overwrite
    # `_input_control.mouse_handler` itself with this wrapper. The wrapper's
    # own fallback then called `_input_control.mouse_handler(ev)`, which by
    # then WAS the wrapper again -- calling itself forever until the stack
    # blew up. This was invisible in our own tests because none of them
    # route a real mouse event through the attribute on the LIVE object
    # after assignment; it only bit on an actual click on real hardware.
    # Fix: grab the ORIGINAL bound method BEFORE reassigning the attribute,
    # so the fallback always reaches prompt_toolkit's real handler, never
    # itself.
    _orig_input_mouse_handler = _input_control.mouse_handler

    def _input_mouse_handler(ev):
        # webbee-code-right-click-everywhere-v1: right-click must paste
        # EVERYWHERE, including the prompt input box itself -- BufferControl's
        # own mouse_handler only understands the LEFT button (click / drag
        # to select / double-click), so a right-click on the input field used
        # to silently do nothing at all (Valentin, live 2026-07-31: "правый
        # клик обязан работать ... даже в поле ввода промпта"). This wraps
        # the stock handler: RIGHT MOUSE_DOWN is intercepted for the SAME
        # _right_paste already wired to the output pane (one paste
        # implementation, now three entry points: pane, prompt, and
        # Ctrl+V) -- click / drag / cursor placement / double-click all
        # still go straight to prompt_toolkit's own handler, untouched.
        from prompt_toolkit.mouse_events import MouseButton, MouseEventType
        if ev.event_type == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.RIGHT:
            _right_paste()
            return None
        return _orig_input_mouse_handler(ev)

    input_win = Window(
        _input_control, height=_input_height, wrap_lines=True, get_line_prefix=_line_prefix)
    input_win.content.mouse_handler = _input_mouse_handler
    toolbar = Window(FormattedTextControl(_toolbar), height=1, always_hide_cursor=True)

    _hover_on = {"v": None}

    def _sync_hover_mode() -> None:
        # ?1003 (any-event mouse = hover) ONLY while Home is active; restore
        # ?1002 (button-event) on leave. Idempotent: writes only on a state
        # change. Teardown's own ?1003l (configure_mouse_modes._disable) is
        # the belt-and-braces cleanup on exit.
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
        if app is None:
            return
        want = (_a().kind == "home")
        if _hover_on["v"] == want:
            return
        out = app.output
        if not hasattr(out, "write_raw"):
            _hover_on["v"] = want
            return
        try:
            if want:
                out.write_raw("\x1b[?1003h")
            else:
                out.write_raw("\x1b[?1003l")
                out.write_raw("\x1b[?1002h")
            out.flush()
        except Exception:
            pass
        _hover_on["v"] = want

    def _switch_to(idx: int) -> None:
        # Tab-bar click -> switch tabs. `slots.switch` already guards a
        # no-op (the clicked tab is already active) and a stale idx (the
        # tab closed between render and release) by returning False -- when
        # it does, neither the history swap nor the redraw happen, so a
        # click on the active tab is a true no-op, never a crash. `prev`
        # captured BEFORE the switch (FIX7b) -- it's the slot we're LEAVING.
        # 0.3.24: stashed UNCONDITIONALLY, before the switch even resolves --
        # harmless on a no-op switch (prev IS the still-active slot, so this
        # is just re-saving its own current text over itself).
        prev = slots.active()
        prev.draft = buf.text
        prev.draft_cursor = buf.cursor_position
        if slots.switch(idx):
            entering = slots.active()
            # Part D: any genuine switch disarms every tab's one-shot
            # busy-close confirm -- an armed "✕?" left over from a click on
            # a DIFFERENT tab (or this one, abandoned) must never linger
            # past the moment the user looks elsewhere.
            disarm_all(slots)
            _swap_history(buf, entering)
            # 0.3.24 (per-tab drafts, product decision -- was FIX7b's "drafts
            # dropped on switch"): a draft mid-type belongs to the tab you
            # typed it into, browser-tab style -- switching away no longer
            # destroys it, it comes right back when you switch back to THIS
            # tab (`_restore_draft`, which still runs the history-load
            # `buf.reset()` first). The leaving slot's own pulled-queue-item
            # carry (↑ pull-to-edit, see _rewrap_pulled) is no longer cleared
            # here either -- it now travels WITH the draft on its own slot,
            # so resubmitting it unedited after a round trip still dedups
            # correctly against a landed twin; `_rewrap_pulled`'s one-shot
            # consume (on the Enter that actually resubmits, in whichever
            # slot is active then) is what retires it, not a switch.
            _restore_draft(buf, entering)
            if on_switch is not None:
                on_switch(idx)
            get_app().invalidate()
            _sync_hover_mode()

    def _close_flow() -> bool:
        # The REAL close flow (Task 5): delegates to webbee.slots.close_active
        # (Home guard, active-idx adjustment, cancel_slot, the post-close
        # note — all PT-free and shared verbatim with repl's `/close`), then
        # invalidates on a genuine close so the tab bar/pane repaint at once.
        # FIX7d: the SURVIVOR (post-close active) slot's own history takes
        # over the shared input buffer, exactly like any other switch — a
        # closed tab's history dies with it, so the buffer must never keep
        # pointing at it. 0.3.24: the buffer now loads the SURVIVOR's own
        # draft (same `_restore_draft` a plain switch uses) instead of a
        # bare reset -- the closed tab's own draft is simply gone with it,
        # nothing to stash (it's not coming back).
        if close_active(slots, cancel_slot):
            survivor = slots.active()
            _swap_history(buf, survivor)
            _restore_draft(buf, survivor)
            get_app().invalidate()
            return True
        return False

    def _close_tab_click(idx: int) -> bool:
        # Task 7 hygiene fix (was: "honest v1" -- clicking ANY ✕ closed the
        # CURRENTLY ACTIVE tab, ignoring which one was actually clicked).
        # `close_at` already resolves the correct post-close active_idx no
        # matter which slot disappears -- the clicked tab itself, one before
        # the active tab, or one after it -- so this needs no branching.
        # Ctrl-W/Ctrl-D/the /close command are UNCHANGED: they have no
        # per-tab idx of their own, so they keep meaning "close what I'm
        # looking at" via `_close_flow`/`close_active` below.
        #
        # Part D (busy-close confirm): a ✕ click on a tab whose OWN turn is
        # still running arms `close_armed` instead of closing outright --
        # the tab bar then renders "✕?" (tabs.tab_fragments) -- and a note
        # lands in THAT tab's own transcript so it's obvious what just
        # happened even if the click landed on a BACKGROUND tab you weren't
        # even looking at. A SECOND click while already armed falls through
        # to the real close below, same as an idle tab's very first click.
        if 0 <= idx < len(slots.slots):
            target = slots.slots[idx]
            if is_turn_alive(target) and not target.close_armed:
                target.close_armed = True
                note = getattr(target.sink, "note", None)
                if note is not None:
                    # 0.3.37: name the KEYBOARD paths too. Mouse reporting is
                    # the one thing that is NOT universal (tmux/screen without
                    # mouse on, some SSH clients, restrictive terminals), so
                    # the confirm note doubles as the discovery point for the
                    # two paths that work everywhere: Ctrl-W and /close.
                    note("tab is busy — click ✕ again, or press Ctrl-W / type "
                         "/close, to close it (the server-side run keeps going)")
                get_app().invalidate()
                return False
        if close_at(slots, idx, cancel_slot):
            survivor = slots.active()
            _swap_history(buf, survivor)         # FIX7d, same as _close_flow above
            _restore_draft(buf, survivor)        # 0.3.24, same as _close_flow above
            get_app().invalidate()
            return True
        return False

    def _new_tab_click() -> None:
        # 0.3.25: the tab bar's + chip -- mirrors `_launch_inject`'s "a mouse
        # handler can't await" shape (fire-and-forget as a background task).
        # `on_new` is the repl's own `_open_new_tab` (async, no args); `None`
        # (no seam wired -- headless/no-dock callers, tests that don't care)
        # is a harmless no-op, same contract `tabs.tab_fragments` already
        # documents for a bare click with nothing wired.
        if on_new is None:
            return
        get_app().create_background_task(on_new())

    # repl-owned hook seam (Task 5, map contract item 5): `/tab`, `/new` and
    # `/close` live in repl.py and only ever mutate `slots` directly -- filled
    # in here so they route through the EXACT same switch/close path as a
    # click or a key (the history swap on every switch, the close note),
    # instead of quietly bypassing it. `ui_hooks=None` (headless/no-dock
    # callers, and every existing test that doesn't pass one) leaves repl's
    # own `ui_hooks.get("switch", slots.switch)` fallback in charge.
    if ui_hooks is not None:
        ui_hooks["switch"] = _switch_to
        ui_hooks["close"] = _close_flow
        # 0.3.37: `/queue edit <n>` needs the LIVE input buffer, which only the
        # dock owns. Exposing the same `_pull_at` the panel's click-to-edit
        # uses means the command path and the mouse path are literally one
        # implementation -- no second buffer-handling code to drift.
        ui_hooks["pull_queued"] = _pull_at
        # /attach (terminal-file-attach-by-path-v1): drop an uploaded file's
        # reference straight into the LIVE input buffer, mirroring how a
        # clipboard paste (_paste_key above) inserts its own "📎 name
        # (file_id=...)" ref -- one insertion mechanism, two entry points.
        def _insert_text(text: str) -> None:
            sep = "" if (not buf.text or buf.text.endswith(" ")) else " "
            buf.insert_text(sep + text + " ")
            get_app().invalidate()
        ui_hooks["insert_text"] = _insert_text
        # FIX3: the Home-spawned first turn seam — repl's `_home_input` uses
        # this so the NEW slot's turn is started through the SAME path a
        # normal Enter-idle submit uses (`slot.turn["task"]` actually gets
        # populated), instead of a bare `await` that ran the turn invisibly
        # (no busy glyph, no Esc/Ctrl-C cancel -- nothing ever recorded it).
        ui_hooks["start_turn_in"] = _start_turn_in
        # Attach-on-poll: the poller's own start-turn seam -- see
        # `_start_attach_in`'s docstring above. Same "no dock -> no entry"
        # fallback contract as start_turn_in: repl's attach_turn wiring
        # awaits the coroutine directly when no dock is present.
        ui_hooks["start_attach_in"] = _start_attach_in

    def _tab_fragments_live():
        # Live like _toolbar/_queue_fragments: re-invoked every redraw, so a
        # status_glyph flip (consent armed in a background tab) or an
        # active-slot change repaints the bar at once. forward=pane.
        # forward_mouse(clamp="top") (FIX6): first refusal on every tab-bar
        # click so a drag armed in the pane just below can still be
        # extended/completed once it releases up here, mirroring the
        # queue/todo panels' own forward=pane.forward_mouse below the pane.
        cols, _rows = sizing.get_size(get_app_or_none())
        return tab_fragments(slots, on_switch=_switch_to, on_close=_close_tab_click,
                             on_new=_new_tab_click, width=cols,
                             forward=lambda ev: _pane().forward_mouse(ev, clamp="top"))

    # The tab bar — pinned at the very TOP, fixed height 1, NEVER hidden
    # (unlike the queue/todo panels below it): it IS the new look, even with
    # only Home showing. focusable=False keeps focus on the input.
    # 0.3.25 (Valentin, live screenshot review): `style="class:tabbar"` seats
    # every chip on its own solid bar (`"tabbar": "bg:#262626"` in the Style
    # dict below) — a browser look, visually separated from the transcript
    # above/below it instead of floating directly on the terminal's own bg.
    tab_bar = Window(FormattedTextControl(_tab_fragments_live, focusable=False),
                     height=1, always_hide_cursor=True, style="class:tabbar")
    # ONE blank row of breathing room between the bar and the transcript —
    # deliberately bare (no style at all): it renders as plain terminal
    # background, transparent-looking, never a second colored stripe.
    tab_bar_spacer = Window(height=1)
    # The single most structural change of the W4a wave (map §3): the pane
    # slot in the root layout is a DynamicContainer, not a bound window —
    # it re-resolves `slots.active().pane.window` on EVERY redraw, so a tab
    # switch repaints a different slot's transcript with no stale reference
    # left over anywhere in the tree.
    pane_container = DynamicContainer(lambda: _pane().window)
    root = HSplit([tab_bar, tab_bar_spacer, pane_container, todo_panel, queue_panel,
                   Frame(input_win), toolbar])
    style = Style.from_dict(_STYLE_DICT)
    app = Application(layout=Layout(root, focused_element=input_win), key_bindings=kb,
                      full_screen=True, mouse_support=True, style=style)
    # Task 5: registering ("escape", "<digit>") chords makes bare Escape a
    # prefix of a longer match, so prompt_toolkit's key-processor now waits
    # up to `timeoutlen` (default 1.0s) before resolving a genuinely LONE
    # Escape (stop-turn / step-clear) when nothing follows it — a real,
    # noticeable regression for a key pressed to stop a turn RIGHT NOW. A
    # true Alt+digit press sends both bytes together (same write, same
    # packet even over SSH), so a much shorter window still disambiguates it
    # correctly; this only shortens the WAIT for a lone Escape, it changes
    # nothing about which binding ultimately fires.
    app.timeoutlen = 0.2
    configure_mouse_modes(app.output)   # ?1002 button-event, never ?1003 any-event
    # Part D: ANY keypress disarms every tab's busy-close confirm, same
    # contract as a tab switch above -- prompt_toolkit's own KeyProcessor
    # fires `after_key_press` for every key it resolves, key binding or
    # plain buffer insert alike, so this is the one universal hook that
    # needs no per-binding wiring at all.
    app.key_processor.after_key_press += lambda _e: disarm_all(slots)

    async def _ticker():
        # animate the spinner + tick the elapsed clock while a turn runs.
        # _tick_once runs UNCONDITIONALLY every tick, busy or idle — a
        # resize while idle must re-wrap the transcript too, and the
        # no-change cost is just two int reads. It resolves the ACTIVE
        # slot's pane itself (slots, not a bound pane) -- _busy_live is the
        # is_busy this ticker feeds it, since the old top-level is_busy
        # param died with the rest of the sink-shaped params.
        while True:
            await asyncio.sleep(_tick_interval(_ticker_busy(slots, _busy_live)))
            _sync_hover_mode()
            _tick_once(slots, app, _busy_live,
                       breathing=lambda: bool(_badge_style_override()))

    # FIX7c (W4a final review — history seeding): the FIRST active slot's
    # own history is pointed at from the START, before a single key is
    # pressed -- not only on the FIRST actual `_switch_to` call. Without
    # this, every line typed pre-switch recorded into the Buffer's own
    # THROWAWAY default `InMemoryHistory()` (never touched `slot.history`,
    # which stayed None); the first later switch away and back then MINTED
    # a brand-new empty history for the slot (`_swap_history`'s own None
    # check), silently losing every line typed before that first switch --
    # ↑ recall on a slot you never left would work, but come back after a
    # Home-and-back and it's gone.
    _swap_history(buf, _a())

    tick = asyncio.ensure_future(_ticker())
    try:
        await app.run_async()
    finally:
        tick.cancel()
    return True
