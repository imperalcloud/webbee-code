import time

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from webbee.banner_art import WEBBEE_CODE

_ICON = {"read_file": "📖", "grep": "🔎", "glob": "🗂️", "write_file": "✎",
         "edit_file": "🔧", "bash": "⚡"}
_BEE = "yellow"       # bee-yellow brand accent
_ACCENT = "cyan"      # prompt / active


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


class RichSink:
    """Rich implementation of TurnSink — the 'Rich inline' look: action lines
    stream top-to-bottom, a transient Live shows a spinner + bottom status
    bar (timer · tools · 🔤 tokens). Not full-screen. In tests pass
    live_enabled=False (no animation) and inject input_fn/clock."""

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
        self._live = None
        self._current = ""
        self._pending = ("", "")

    # ---- welcome ------------------------------------------------------------
    def welcome(self, account, cwd: str, surface: str) -> None:
        """One-time launch welcome: CLEAR the screen so webbee owns the window,
        then a centered WEBBEE CODE logo + imperal.io + an honest account panel
        (who/plan/tier/member-since). Everything is centered to the terminal
        width as one splash — nothing floats. Missing fields omit their row;
        signed-out shows only the /login hint."""
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
        self.console.print()   # breathing room between the user's message and the response
        self._arm_live()

    def end_turn(self, final_text: str) -> None:
        self._stop_live()
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

    def note(self, message: str) -> None:
        self._stop_live()
        self.console.print(Text("  " + message, style=_BEE))

    def clear(self) -> None:
        """/clear: wipe the screen + reset the session counters (tokens,
        cost, tools). Does NOT touch _started — that's turn-scoped."""
        self.console.clear()
        self.tokens = 0
        self.cost_usd = 0.0
        self.session_tokens = 0
        self.session_cost = 0.0
        self._tools = 0
        self._current = ""

    def abort(self) -> None:
        """Ctrl-C mid-turn: stop any running Live cleanly (restores the
        cursor). No printing — the caller (repl.py) prints the note."""
        self._stop_live()

    # ---- TurnSink -------------------------------------------------------
    def tool_start(self, tool: str, args: dict) -> None:
        self._tools += 1
        arg = args.get("path") or args.get("pattern") or args.get("command") or ""
        self._pending = (tool, str(arg))
        self._current = f"{tool} {str(arg)[:40]}".strip()
        self._refresh()  # the completed full-width row is printed in tool_result

    def tool_result(self, tool: str, ok: bool, summary: str) -> None:
        # One full-width row: icon + tool (+arg) on the left, ✓/✗ + summary
        # pinned to the right edge. Width is the live terminal width (dynamic).
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
        self._print_above(row)
        self._pending = ("", "")
        self._refresh()

    def ask_consent(self, app_id: str, tool: str, args: dict) -> str:
        """Render a prompt and return the user's RAW reply (trimmed only).
        NEVER interpret — the kernel brain decides (ICNLI)."""
        self._stop_live()
        label = f"{app_id}·{tool}" if app_id else tool
        sal = _salient_arg(args)
        self.console.print(Text.assemble(("  ? approve ", "yellow"), (label, "dim"),
                                          (("  " + sal[:60]) if sal else "", "dim")))
        raw = self._input("     ")
        self._arm_live()  # re-arm spinner for the rest of the turn
        return raw.strip()

    def panel_release(self, panel_url: str, summary: str) -> None:
        self._stop_live()
        body = Text.assemble(
            (summary + "\n\n" if summary else "", "white"),
            ("Approve it in your browser:\n", "white"),
            (f"  {panel_url}\n", f"bold {_ACCENT}"),
            ("Then ask again — you weren't charged.", "dim"),
        )
        self.console.print(Panel(body, title="💳 This costs money", border_style="magenta"))
        self._arm_live()

    def progress(self, text: str) -> None:
        if text:
            self._print_above(Text("  " + text, style="dim italic"))

    def plan_blocked(self, tool: str) -> None:
        """Plan mode auto-declines writes/destructive. Tell the user WHY and
        how to allow it (Shift+Tab) — never silently do nothing. Autopilot and
        default never reach this."""
        self._stop_live()
        self.console.print(Text.assemble(
            ("  ⛔ plan mode", _BEE),
            (f" — {tool} blocked. " if tool else " — action blocked. ", "dim"),
            ("Press Shift+Tab to switch to default or autopilot to allow it.", "dim")))
        self._arm_live()

    def usage(self, tokens: int, cost_usd: float) -> None:
        # Cumulative frame — trust the server's running totals verbatim.
        self.tokens = tokens
        self.cost_usd = cost_usd
        self._refresh()

    # ---- internals ------------------------------------------------------
    def _status(self):
        label = self._current or "Thinking"
        bar = Text(f"  ◷ {self._elapsed():.0f}s · ⚡ {self._tools} · 🔤 {_fmt_tokens(self.tokens)} tok",
                   style="dim")
        return Group(Spinner("dots", text=Text(" " + label + "…", style=_ACCENT)), bar)

    def _elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return self._clock() - self._started

    def _arm_live(self) -> None:
        """Start a fresh transient Live (spinner + status bar). Always stops
        any previously-running Live first (a second begin_turn() without
        cleanup would otherwise leak the previous Live's thread and freeze
        the bar). No-op when live is disabled (tests) — all console.print
        calls still fire."""
        self._stop_live()
        if not self._live_enabled:
            return
        from rich.live import Live
        self._live = Live(self._status(), console=self.console,
                          refresh_per_second=8, transient=True)
        self._live.start()

    def _print_above(self, renderable) -> None:
        # Live is always built with console=self.console, so printing to the
        # shared console prints cleanly above the live region too.
        self.console.print(renderable)

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._status())

    def _stop_live(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None
