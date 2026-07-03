import time

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.text import Text

from webbee.banner_art import WEBBEE_CODE

_ICON = {"read_file": "📖", "grep": "🔎", "glob": "🗂️", "write_file": "✎",
         "edit_file": "🔧", "bash": "⚡"}
_BEE = "yellow"       # bee-yellow brand accent
_ACCENT = "cyan"      # prompt / active


def _fmt_tokens(n: int) -> str:
    """Compact token count: 2100 -> '2.1k', 900 -> '900'."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


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

    # ---- welcome ------------------------------------------------------------
    def welcome(self, account, cwd: str, surface: str) -> None:
        """One-time launch welcome: WEBBEE CODE ASCII + imperal.io + an honest
        account panel (who/plan/tier/member-since). Missing fields simply omit
        their row; signed-out shows only the /login hint."""
        self.console.print()
        self.console.print(Text(WEBBEE_CODE + "  🐝", style=f"bold {_BEE}"))
        self.console.print(Text("·  i m p e r a l . i o  ·".center(self.console.width), style=_ACCENT))
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
        for label, value in rows:
            self.console.print(Text.assemble(("   " + label.ljust(14), "dim"), (value, "white")))
        self.console.print()
        self.console.print(Text("   /help · Ctrl-D to exit", style="dim"))
        self.console.print()

    # ---- turn lifecycle -------------------------------------------------
    def begin_turn(self) -> None:
        self._tools = 0
        self._started = self._clock()
        self._current = ""
        self._arm_live()

    def end_turn(self, final_text: str) -> None:
        self._stop_live()
        if final_text:
            self.console.print(Text("  🐝", style=f"bold {_BEE}"))
            self.console.print(Markdown(final_text))
        elapsed = self._elapsed()
        self.session_tokens += self.tokens
        self.session_cost += self.cost_usd
        noun = "action" if self._tools == 1 else "actions"
        self.console.print(Rule(style="dim"))
        self.console.print(Text(
            f"  ◷ {elapsed:.1f}s · ⚡ {self._tools} {noun} · 🔤 {_fmt_tokens(self.tokens)} tok"
            f"  (session {_fmt_tokens(self.session_tokens)})",
            style="dim"))

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
        icon = _ICON.get(tool, "⚡")
        arg = args.get("path") or args.get("pattern") or args.get("command") or ""
        self._current = f"{tool} {str(arg)[:40]}".strip()
        self._print_above(Text.assemble(("  " + icon + " ", ""), (tool, f"bold {_ACCENT}"),
                                         (("  " + str(arg)[:80]) if arg else "", "white")))
        self._refresh()

    def tool_result(self, tool: str, ok: bool, summary: str) -> None:
        mark = "✓" if ok else "✗"
        style = "green" if ok else "red"
        self._print_above(Text.assemble(("     ", ""), (mark + " ", style),
                                         (str(summary)[:80], "dim")))
        self._refresh()

    def ask_consent(self, app_id: str, tool: str, args: dict) -> str:
        """Render a prompt and return the user's RAW reply (trimmed only).
        NEVER interpret — the kernel brain decides (ICNLI)."""
        self._stop_live()
        label = f"{app_id}.{tool}" if app_id else tool
        self.console.print(Text.assemble(("  ❓ ", "yellow"), (label, "bold yellow"),
                                          ("  " + str(args)[:80], "dim")))
        raw = self._input("     approve? ")
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
