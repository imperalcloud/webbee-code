import asyncio
import time

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from webbee.banner_art import WEBBEE_CODE

_ICON = {"read_file": "📖", "grep": "🔎", "glob": "🗂️", "write_file": "✎",
         "edit_file": "🔧", "bash": "⚡"}
_BEE = "yellow"       # bee-yellow brand accent — logo / 🐝 / notes / busy dot ONLY
_ACCENT = "cyan"      # interactive chrome ONLY — live caret / mode / panel url


def _fmt_tokens(n: int) -> str:
    """Compact token count: 2100 -> '2.1k', 900 -> '900'."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def _salient_arg(args: dict) -> str:
    """The one human-readable arg to show for a tool/consent call (PURE) — a
    title/path/command/name over an opaque id; falls back to the first id-ish
    value. Keeps the feed readable instead of dumping a raw args dict."""
    if not isinstance(args, dict):
        return ""
    for k in ("title", "name", "path", "pattern", "command", "query", "q"):
        v = args.get(k)
        if v:
            return str(v)
    for k in ("id", "note_id", "folder_id", "task_id"):
        v = args.get(k)
        if v:
            return str(v)
    return ""


def _invalidate() -> None:
    """Redraw the pinned prompt_toolkit dock if one is running. No-op when no
    app is active (tests / non-tty fallback) — so the sink works both ways."""
    try:
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
        if app is not None:
            app.invalidate()
    except Exception:
        pass


class RichSink:
    """Rich implementation of TurnSink. Turn output (action feed, 🐝 answer,
    footer) is printed via the Rich Console; when a persistent prompt_toolkit
    dock is running (repl wraps it in patch_stdout), those prints land ABOVE
    the pinned input box as real scrollback. The live status (busy/elapsed/
    tokens) is NOT a Rich spinner anymore — it lives in the dock toolbar, fed
    by status(); this sink just holds the state and calls _invalidate() to
    nudge a redraw. Consent runs through the dock via an asyncio.Future
    (resolve_consent), with a sync input fallback for non-tty.

    Tests pass live_enabled=False + inject input_fn/clock; with no running app
    _invalidate() is a no-op and consent uses the injected input_fn."""

    def __init__(self, console=None, *, live_enabled=True, input_fn=input, clock=time.monotonic):
        self.console = console or Console()
        self._live_enabled = live_enabled
        self._input = input_fn
        self._clock = clock
        self.tokens = 0
        self.cost_usd = 0.0
        self.session_tokens = 0
        self.session_cost = 0.0
        self._tools = 0
        self._started = None
        self._busy = False
        self._current = ""
        self._pending = ("", "")
        self._consent = None            # asyncio.Future while awaiting a reply
        self._consent_summary = ""

    # ---- welcome ------------------------------------------------------------
    def welcome(self, account, cwd: str, surface: str) -> None:
        """One-time launch welcome: CLEAR the screen so webbee owns the window,
        then a centered WEBBEE CODE logo + imperal.io + an honest account panel
        (who/plan/tier/member-since). Runs BEFORE the persistent dock starts."""
        self.console.clear()
        w = self.console.width

        def _center_block(text: str) -> str:
            lines = text.splitlines()
            bw = max((len(line) for line in lines), default=0)
            pad = " " * max(0, (w - bw) // 2)
            return "\n".join(pad + line for line in lines)

        self.console.print()
        self.console.print(Text(_center_block(WEBBEE_CODE), style=f"bold {_BEE}"))
        self.console.print(Text("🐝".center(w), style=f"bold {_BEE}"))
        self.console.print(Text("ICNLI AI Cloud OS · Agent".center(w), style=f"bold {_ACCENT}"))
        self.console.print(Text("·  i m p e r a l . i o  ·".center(w), style="dim"))
        self.console.print()
        rows = []
        if account.signed_in:
            who = account.email + (f"   ·   @{account.nickname}" if account.nickname else "")
            rows.append(("Signed in as", who))
            if account.plan:
                plan = account.plan + (f" · {account.plan_status}" if account.plan_status else "")
                plan += (f" · renews {account.plan_renews}" if account.plan_renews else "")
                rows.append(("Plan", plan))
            if account.dev_tier:
                rows.append(("Developer", f"{account.dev_tier} tier"))
            if account.member_since:
                rows.append(("Member since", account.member_since))
        else:
            rows.append(("", "not signed in — /login"))
        bw = max((len(label.ljust(14) + value) for label, value in rows), default=0)
        pad = " " * max(0, (w - bw) // 2)
        for label, value in rows:
            self.console.print(Text.assemble((pad + label.ljust(14), "dim"), (value, "white")))
        self.console.print()
        self.console.print(Text("/help · Ctrl-D to exit".center(w), style="dim"))
        self.console.print()

    # ---- turn lifecycle -------------------------------------------------
    def begin_turn(self) -> None:
        self._tools = 0
        self._started = self._clock()
        self._current = ""
        self._pending = ("", "")
        self._busy = True
        self.console.print()   # breathing room between the user's message and the response
        _invalidate()

    def end_turn(self, final_text: str) -> None:
        self._busy = False
        if final_text:
            self.console.print()   # separation before the focus block
            self.console.print(Text("  🐝", style=f"bold {_BEE}"))
            self.console.print(Markdown(final_text))
        elapsed = self._elapsed()
        self.session_tokens += self.tokens
        self.session_cost += self.cost_usd
        noun = "action" if self._tools == 1 else "actions"
        self.console.print(Text(
            f"  {elapsed:.1f}s · {self._tools} {noun} · {_fmt_tokens(self.tokens)} tok"
            f" · session {_fmt_tokens(self.session_tokens)} tok",
            style="dim"))
        self.console.print()   # breathing room before the next prompt
        _invalidate()

    def note(self, message: str) -> None:
        self.console.print(Text("  " + message, style=_BEE))
        _invalidate()

    def user_echo(self, text: str) -> None:
        """Commit the just-sent user message as a quiet, UN-boxed dim line so
        past turns read as calm scrollback — only the LIVE input is bordered."""
        self.console.print(Text("  ❯ " + text, style="dim"))

    def clear(self) -> None:
        """/clear: wipe the screen + reset the session counters."""
        self.console.clear()
        self.tokens = 0
        self.cost_usd = 0.0
        self.session_tokens = 0
        self.session_cost = 0.0
        self._tools = 0
        self._current = ""
        _invalidate()

    def abort(self) -> None:
        """Ctrl-C mid-turn: clear busy so the toolbar drops back to idle. No
        printing — the caller (repl.py) prints the note."""
        self._busy = False
        _invalidate()

    # ---- TurnSink -------------------------------------------------------
    def tool_start(self, tool: str, args: dict) -> None:
        self._tools += 1
        arg = args.get("path") or args.get("pattern") or args.get("command") or ""
        self._pending = (tool, str(arg))
        self._current = f"{tool} {str(arg)[:40]}".strip()
        _invalidate()  # the completed full-width row is printed in tool_result

    def tool_result(self, tool: str, ok: bool, summary: str) -> None:
        # One calm dim full-width row: icon + tool (+arg) left, ✓/✗ + summary
        # right. Only the ✓/✗ carries colour — the rest recedes (dim).
        _tool, arg = self._pending if self._pending[0] else (tool, "")
        icon = _ICON.get(_tool, "⚡")
        mark = "✓" if ok else "✗"
        left = Text.assemble(("  " + icon + " ", "dim"), (_tool, "dim"),
                             (("  " + arg[:48]) if arg else "", "dim"))
        right = Text.assemble((mark + " ", "green" if ok else "red"),
                              (str(summary)[:60], "dim"), ("  ", ""))
        row = Table.grid(expand=True, padding=0)
        row.add_column(justify="left", ratio=1, no_wrap=True)
        row.add_column(justify="right", no_wrap=True)
        row.add_row(left, right)
        self.console.print(row)
        self._pending = ("", "")
        _invalidate()

    async def ask_consent(self, app_id: str, tool: str, args: dict) -> str:
        """Ask for consent and return the user's RAW reply (trimmed only) —
        NEVER interpret (the kernel decides, ICNLI). When the dock is running
        the reply comes through the pinned box via an asyncio.Future (no
        blocking input on the event loop); otherwise fall back to the injected
        sync reader (tests / non-tty)."""
        label = f"{app_id}·{tool}" if app_id else tool
        sal = _salient_arg(args)
        self.console.print(Text.assemble(("  ? approve ", "yellow"), (label, "dim"),
                                          (("  " + sal[:60]) if sal else "", "dim")))
        fut = self._arm_consent(label, sal)
        if fut is None:                       # non-tty / no running app
            raw = self._input("     ")
        else:
            raw = await fut
        self._consent = None
        self._consent_summary = ""
        raw = (raw or "").strip()
        self.console.print(Text("  ↳ " + raw, style="dim"))   # quiet echo of the reply
        _invalidate()
        return raw

    def panel_release(self, panel_url: str, summary: str) -> None:
        body = Text.assemble(
            (summary + "\n\n" if summary else "", "white"),
            ("Approve it in your browser:\n", "white"),
            (f"  {panel_url}\n", f"bold {_ACCENT}"),
            ("Then ask again — you weren't charged.", "dim"),
        )
        self.console.print(Panel(body, title="💳 This costs money", border_style="magenta"))
        _invalidate()

    def progress(self, text: str) -> None:
        if text:
            self.console.print(Text("  " + text, style="dim italic"))

    def plan_blocked(self, tool: str) -> None:
        """Plan mode auto-declines writes/destructive. Tell the user WHY and how
        to allow it (Shift+Tab). Autopilot and default never reach this."""
        self.console.print(Text.assemble(
            ("  ⛔ plan mode", _BEE),
            (f" — {tool} blocked. " if tool else " — action blocked. ", "dim"),
            ("Press Shift+Tab to switch to default or autopilot to allow it.", "dim")))
        _invalidate()

    def usage(self, tokens: int, cost_usd: float) -> None:
        # Cumulative frame — trust the server's running totals verbatim.
        self.tokens = tokens
        self.cost_usd = cost_usd
        _invalidate()

    # ---- dock bridge (read by tui.run_session's toolbar + Enter binding) ---
    def status(self) -> dict:
        """Live state for the dock toolbar."""
        return {"busy": self._busy, "current": self._current,
                "elapsed": self._elapsed(), "tools": self._tools,
                "tokens": self.tokens, "cost": self.cost_usd,
                "consent": self.consent_pending()}

    def is_busy(self) -> bool:
        return self._busy

    def consent_pending(self) -> bool:
        return self._consent is not None and not self._consent.done()

    def resolve_consent(self, raw: str) -> None:
        """Called by the dock's Enter binding when a consent reply is awaited —
        hands the RAW reply verbatim to the awaiting ask_consent (ICNLI)."""
        if self.consent_pending():
            self._consent.set_result(raw)

    # ---- internals ------------------------------------------------------
    def _arm_consent(self, label: str, summary: str):
        """Create the consent Future iff a dock is running; else None (caller
        falls back to sync input)."""
        try:
            from prompt_toolkit.application import get_app_or_none
            if get_app_or_none() is None:
                return None
            self._consent = asyncio.get_running_loop().create_future()
            self._consent_summary = summary or label
            _invalidate()
            return self._consent
        except Exception:
            return None

    def _elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return self._clock() - self._started
