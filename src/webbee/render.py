import asyncio
import time

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
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
    """Redraw the running prompt_toolkit prompt (its bottom_toolbar) if one is
    active, so the status line reflects the latest sink state between ticker
    ticks. No-op when none is active (tests / non-tty fallback)."""
    try:
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
        if app is not None:
            app.invalidate()
    except Exception:
        pass


class RichSink:
    """Rich implementation of TurnSink for the INLINE terminal. Turn output
    (action feed, 🐝 answer, footer, progress, streaming) is printed by a real
    Rich Console straight to stdout; under the inline PromptSession loop the
    prompt_toolkit patch_stdout() proxy commits each print into the terminal's
    NATIVE scrollback ABOVE the prompt (so selection / copy / scrollback / tmux
    all belong to the terminal, not to us). The live status (busy/elapsed/
    tokens) is the PromptSession's bottom_toolbar, fed by status(); after each
    render this sink calls _nudge() to refresh it. Consent runs through the
    prompt via an asyncio.Future (resolve_consent), with a sync input fallback
    for non-tty.

    Tests pass live_enabled=False + inject input_fn/clock; then _nudge() is a
    harmless no-op and consent uses the injected input_fn."""

    def __init__(self, console=None, *, live_enabled=True, input_fn=input,
                 clock=time.monotonic):
        self.console = console or Console()
        self._live_enabled = live_enabled
        self._input = input_fn
        self._clock = clock
        self.tokens = 0
        self.credits = 0
        self.session_tokens = 0
        self.session_credits = 0
        self._tools = 0
        self._started = None
        self._busy = False
        self._current = ""
        self._pending = ("", "")
        self._consent = None            # asyncio.Future while awaiting a reply
        self._consent_summary = ""

    def _nudge(self) -> None:
        """After output/state changes: refresh the bottom_toolbar (if a prompt
        is live). Inline output is already committed to scrollback by print."""
        _invalidate()

    # ---- welcome ------------------------------------------------------------
    def welcome(self, account, cwd: str, surface: str) -> None:
        """One-time launch splash: a centered WEBBEE CODE logo + imperal.io + an
        honest account panel (who/plan/tier/member-since). Prints to stdout
        normally (inline model) — runs BEFORE the prompt loop, so it lands in
        the native scrollback. NEVER clears the screen (the terminal owns its
        scrollback)."""
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
        self._nudge()

    # ---- turn lifecycle -------------------------------------------------
    def begin_turn(self) -> None:
        self._tools = 0
        self._started = self._clock()
        self._current = ""
        self._pending = ("", "")
        self.tokens = 0        # per-turn live counters (usage frames are per-turn cumulative)
        self.credits = 0
        self._busy = True
        self.console.print()   # breathing room between the user's message and the response
        self._nudge()

    def end_turn(self, final_text: str) -> None:
        self._busy = False
        if final_text:
            self.console.print()   # separation before the focus block
            self.console.print(Text("  🐝 Webbee", style=f"bold {_BEE}"))
            self.console.print(Markdown(final_text))
        elapsed = self._elapsed()
        self.session_tokens += self.tokens
        self.session_credits += self.credits
        noun = "action" if self._tools == 1 else "actions"
        self.console.print(Text(
            f"  {elapsed:.1f}s · {self._tools} {noun} · {_fmt_tokens(self.tokens)} tok"
            f" · session {_fmt_tokens(self.session_tokens)} tok",
            style="dim"))
        self.console.print()   # breathing room before the next prompt
        self._nudge()

    def note(self, message: str) -> None:
        self.console.print(Text("  " + message, style=_BEE))
        self._nudge()

    def user_echo(self, text: str) -> None:
        """Commit the just-sent user message as its own clearly-readable line
        with a background bar (NOT boxed like the live input) so it stands out
        as 'what I sent' in the scrollback."""
        self.console.print(Text.assemble(
            ("  ", ""), (" ❯ " + text + " ", "bold white on grey30")))
        self._nudge()

    def clear(self) -> None:
        """/clear: reset the session counters. Inline model — NEVER wipes the
        native scrollback (the terminal owns it); prints a light separator so
        the eye registers a fresh start instead."""
        self.console.print(Text("  ──────── cleared ────────", style="dim"))
        self.tokens = 0
        self.credits = 0
        self.session_tokens = 0
        self.session_credits = 0
        self._tools = 0
        self._current = ""
        self._nudge()

    def abort(self) -> None:
        """Ctrl-C mid-turn: clear busy so the toolbar drops back to idle. No
        printing — the caller (repl.py) prints the note."""
        self._busy = False
        self._nudge()

    # ---- TurnSink -------------------------------------------------------
    def tool_start(self, tool: str, args: dict) -> None:
        self._tools += 1
        arg = args.get("path") or args.get("pattern") or args.get("command") or ""
        self._pending = (tool, str(arg))
        self._current = f"{tool} {str(arg)[:40]}".strip()
        self._nudge()  # the completed line is printed in tool_result

    def tool_result(self, tool: str, ok: bool, summary: str) -> None:
        # One calm dim line: icon + tool (+arg), then the ✓/✗ RIGHT NEXT TO the
        # action (not pinned to the far right), then a dim summary. Only the
        # ✓/✗ carries colour — the rest recedes (dim).
        _tool, arg = self._pending if self._pending[0] else (tool, "")
        icon = _ICON.get(_tool, "⚡")
        mark = "✓" if ok else "✗"
        self.console.print(Text.assemble(
            ("  " + icon + " ", "dim"),
            (_tool, "dim"),
            (("  " + arg[:40]) if arg else "", "dim"),
            ("   ", ""),
            (mark + " ", "green" if ok else "red"),
            (str(summary)[:50], "dim"),
        ))
        self._pending = ("", "")
        self._nudge()

    async def ask_consent(self, app_id: str, tool: str, args: dict) -> str:
        """Ask for consent and return the user's RAW reply (trimmed only) —
        NEVER interpret (the kernel decides, ICNLI). When the prompt loop is
        running the reply comes through the next accepted line via an
        asyncio.Future (no blocking input on the event loop); otherwise fall
        back to the injected sync reader (tests / non-tty)."""
        label = f"{app_id}·{tool}" if app_id else tool
        sal = _salient_arg(args)
        self.console.print(Text.assemble(("  ? approve ", "yellow"), (label, "dim"),
                                          (("  " + sal[:60]) if sal else "", "dim")))
        fut = self._arm_consent(label, sal)
        if fut is None:                       # non-tty / no running app
            raw = self._input("     ")
        else:
            self._nudge()
            raw = await fut
        self._consent = None
        self._consent_summary = ""
        raw = (raw or "").strip()
        self.console.print(Text("  ↳ " + raw, style="dim"))   # quiet echo of the reply
        self._nudge()
        return raw

    def panel_release(self, panel_url: str, summary: str) -> None:
        body = Text.assemble(
            (summary + "\n\n" if summary else "", "white"),
            ("Approve it in your browser:\n", "white"),
            (f"  {panel_url}\n", f"bold {_ACCENT}"),
            ("Then ask again — you weren't charged.", "dim"),
        )
        self.console.print(Panel(body, title="💳 This costs money", border_style="magenta"))
        self._nudge()

    def login_prompt(self, user_code: str, url: str) -> None:
        """Device-code sign-in: show the URL to open + the code to enter, as a
        clear framed block. A bare print would be invisible in the full-screen
        dock, so this renders into the feed; the terminal then polls silently
        until you authorize in the browser."""
        body = Text.assemble(
            ("Open this URL in any browser (a phone is fine):\n", "white"),
            (f"  {url}\n\n", f"bold {_ACCENT}"),
            ("and enter this code:\n", "white"),
            (f"  {user_code}\n\n", f"bold {_BEE}"),
            ("Waiting for you to authorize…", "dim"),
        )
        self.console.print(Panel(body, title="🐝 Connect this terminal", border_style=_BEE))
        self._nudge()

    def sessions_table(self, sessions) -> None:
        """Active sessions as a compact table (English-only): #, session, IP,
        last-seen, and a bee-yellow 'this device' marker for the current one."""
        from rich.table import Table
        self.console.print(Text("  Active sessions", style=f"bold {_BEE}"))
        if not sessions:
            self.console.print(Text("  (none)", style="dim"))
            self._nudge()
            return
        t = Table(show_header=True, header_style="dim", box=None, padding=(0, 3, 0, 0))
        t.add_column("#", style="dim", justify="right")
        t.add_column("session")
        t.add_column("ip", style="dim")
        t.add_column("last seen", style="dim")
        t.add_column("")
        for i, s in enumerate(sessions, 1):
            label = str(s.get("label") or s.get("surface") or "?")
            ip = str(s.get("ip_address") or "-")
            seen = str(s.get("last_seen_at") or "")[:16].replace("T", " ")
            here = Text("this device", style=f"bold {_BEE}") if s.get("current") else Text("")
            t.add_row(str(i), label, ip, seen, here)
        self.console.print(t)
        self.console.print(Text("  /sessions revoke <#>  ·  /logout-others", style="dim"))
        self._nudge()

    def step_detail(self, detail: dict) -> None:
        """P1b drill-down: one bordered block — facts row + bounded
        args/result previews (already PII-masked server-side)."""
        def _prev(p):
            p = p or {}
            note = f"  (truncated, {p.get('total_bytes', 0)} bytes total)" if p.get("truncated") else ""
            return (str(p.get("preview", "") or "")[:2000]) + note

        mark = "✓" if detail.get("ok") else "✗"
        head = (f"{mark} {detail.get('app_id', '')}·{detail.get('tool', '')}  "
                f"{(detail.get('duration_ms', 0) or 0) / 1000:.1f}s  "
                f"trace {detail.get('trace_id', '')}")
        body = f"{head}\n\nargs:\n{_prev(detail.get('args'))}\n\nresult:\n{_prev(detail.get('result'))}"
        self.console.print(Panel(body, border_style="dim", title="step detail", title_align="left"))
        self._nudge()

    def progress(self, text: str) -> None:
        if text:
            self.console.print(Text("  " + text, style="dim italic"))
            self._nudge()

    def plan_blocked(self, tool: str) -> None:
        """Plan mode auto-declines writes/destructive. Tell the user WHY and how
        to allow it (Shift+Tab). Autopilot and default never reach this."""
        self.console.print(Text.assemble(
            ("  ⛔ plan mode", _BEE),
            (f" — {tool} blocked. " if tool else " — action blocked. ", "dim"),
            ("Press Shift+Tab to switch to default or autopilot to allow it.", "dim")))
        self._nudge()

    def usage(self, tokens: int, credits: int) -> None:
        # Cumulative frame — trust the server's running totals verbatim.
        # Slice C: CREDITS (not raw $) — the kernel keeps the $ server-side.
        self.tokens = tokens
        self.credits = credits
        self._nudge()

    # ---- prompt bridge (read by tui.run_session's bottom_toolbar + dispatch) ---
    def status(self) -> dict:
        """Live state for the prompt's bottom_toolbar. The counter shows SESSION
        TOTAL (not per-turn): the running session total, plus the in-flight
        turn's spend while busy (so it grows live), with no double-count at idle
        (end_turn has already folded the finished turn into the session)."""
        return {"busy": self._busy, "current": self._current,
                "elapsed": self._elapsed(), "tools": self._tools,
                "tokens": self.session_tokens + (self.tokens if self._busy else 0),
                "credits": self.session_credits + (self.credits if self._busy else 0),
                "consent": self.consent_pending()}

    def is_busy(self) -> bool:
        return self._busy

    def consent_pending(self) -> bool:
        return self._consent is not None and not self._consent.done()

    def resolve_consent(self, raw: str) -> None:
        """Called by the prompt loop when an accepted line answers a pending
        consent — hands the RAW reply verbatim to the awaiting ask_consent
        (ICNLI)."""
        if self.consent_pending():
            self._consent.set_result(raw)

    # ---- internals ------------------------------------------------------
    def _arm_consent(self, label: str, summary: str):
        """Create the consent Future iff a prompt app is running; else None
        (caller falls back to sync input)."""
        try:
            from prompt_toolkit.application import get_app_or_none
            if get_app_or_none() is None:
                return None
            self._consent = asyncio.get_running_loop().create_future()
            self._consent_summary = summary or label
            return self._consent
        except Exception:
            return None

    def _elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return self._clock() - self._started
