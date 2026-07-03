import time

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

_ICON = {"read_file": "📖", "grep": "🔎", "glob": "🗂️", "write_file": "✎",
         "edit_file": "🔧", "bash": "⚡"}


class RichSink:
    """Rich implementation of TurnSink — the 'Rich inline' look: action lines
    stream top-to-bottom, a transient Live shows a spinner + bottom status
    bar (timer · tools · 🪙 credits). Not full-screen. In tests pass
    live_enabled=False (no animation) and inject input_fn/clock."""

    def __init__(self, console=None, *, live_enabled=True, input_fn=input, clock=time.monotonic):
        self.console = console or Console()
        self._live_enabled = live_enabled
        self._input = input_fn
        self._clock = clock
        self.credits = 0
        self._tools = 0
        self._started = 0.0
        self._live = None

    # ---- turn lifecycle -------------------------------------------------
    def begin_turn(self) -> None:
        self._tools = 0
        self._started = self._clock()
        self._arm_live()

    def end_turn(self, final_text: str) -> None:
        self._stop_live()
        if final_text:
            self.console.print(Markdown(final_text))
        elapsed = self._clock() - self._started
        summary = Text(f"◷ {elapsed:.1f}s   ⛁ {self._tools} действия   🪙 {self.credits} credits",
                       style="dim")
        self.console.print(Text("─" * 46, style="dim"))
        self.console.print(summary)

    def note(self, message: str) -> None:
        self._stop_live()
        self.console.print(Text(message, style="yellow"))

    # ---- TurnSink -------------------------------------------------------
    def tool_start(self, tool: str, args: dict) -> None:
        self._tools += 1
        icon = _ICON.get(tool, "•")
        arg = args.get("path") or args.get("pattern") or args.get("command") or ""
        self._print_above(Text.assemble((f" {icon} ", ""), (tool, "bold cyan"),
                                         ("  " + str(arg)[:80], "white")))
        self._refresh()

    def tool_result(self, tool: str, ok: bool, summary: str) -> None:
        mark = "✓" if ok else "✗"
        style = "green" if ok else "red"
        self._print_above(Text.assemble(("   └─ ", "dim"), (mark + " ", style),
                                         (summary, "dim")))
        self._refresh()

    def ask_consent(self, app_id: str, tool: str, args: dict) -> str:
        """Render a prompt and return the user's RAW reply (trimmed only).
        NEVER interpret — the kernel brain decides (ICNLI)."""
        self._stop_live()
        label = f"{app_id}.{tool}" if app_id else tool
        self.console.print(Text.assemble(("❓ ", "yellow"), (label, "bold yellow"),
                                          ("  " + str(args)[:80], "dim")))
        raw = self._input("   approve? ")
        self._arm_live()  # re-arm spinner for the rest of the turn
        return raw.strip()

    def panel_release(self, panel_url: str, summary: str) -> None:
        self._stop_live()
        body = Text.assemble(
            (summary + "\n\n" if summary else "", "white"),
            ("Подтверди в браузере:\n", "white"),
            (f"  {panel_url}\n", "bold cyan"),
            ("Потом попроси снова — с тебя не списали.", "dim"),
        )
        self.console.print(Panel(body, title="💳 Это стоит денег", border_style="magenta"))
        self._arm_live()

    def progress(self, text: str) -> None:
        if text:
            self._print_above(Text(text, style="dim italic"))

    def usage(self, credits: int, tokens: int, cumulative_credits: int) -> None:
        # Trust the server's running total when present; else accumulate.
        self.credits = cumulative_credits or (self.credits + credits)
        self._refresh()

    # ---- internals ------------------------------------------------------
    def _status(self):
        bar = Text(f"  ◷ {self._elapsed():.0f}s   ⛁ {self._tools}   🪙 {self.credits} credits",
                   style="dim")
        return Group(Spinner("dots", text=Text(" Думаю…", style="cyan")), bar)

    def _elapsed(self) -> float:
        return self._clock() - self._started

    def _arm_live(self) -> None:
        """Start a fresh transient Live (spinner + status bar). No-op when
        live is disabled (tests) — all console.print calls still fire."""
        if not self._live_enabled:
            return
        from rich.live import Live
        self._live = Live(self._status(), console=self.console,
                          refresh_per_second=8, transient=True)
        self._live.start()

    def _print_above(self, renderable) -> None:
        # Rich Live prints via its own console above the live region cleanly.
        (self._live.console if self._live else self.console).print(renderable)

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._status())

    def _stop_live(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None
