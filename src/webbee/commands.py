from dataclasses import dataclass

_MODES = ("default", "plan", "autopilot")

_HELP = """Commands:
  /help              show this help
  /login             sign in to your Imperal account (browser)
  /logout            sign out and remove local credentials
  /clear             clear the screen + reset session counters
  /mode [default|plan|autopilot]   consent mode (no arg — show current)
  /cost  (=/usage)   tokens + $ cost this session
  /status            cwd · git · surface · tokens · version
  /exit  (=/quit)    quit"""


@dataclass(frozen=True)
class CommandContext:
    mode: str
    workspace: str
    version: str
    surface: str
    logged_in: bool
    tokens: int
    cost_usd: float
    git_branch: str


@dataclass(frozen=True)
class SlashResult:
    handled: bool
    exit: bool = False
    message: str = ""
    action: str = ""
    new_mode: "str | None" = None


def dispatch(line: str, ctx: CommandContext) -> SlashResult:
    """Parse one input line. Non-slash lines return handled=False (the REPL
    then sends them to the agent). Slash lines are fully handled here."""
    text = line.strip()
    if not text.startswith("/"):
        return SlashResult(handled=False)

    parts = text.split()
    cmd, args = parts[0].lower(), parts[1:]

    if cmd in ("/exit", "/quit"):
        return SlashResult(handled=True, exit=True)
    if cmd == "/help":
        return SlashResult(handled=True, action="help", message=_HELP)
    if cmd == "/login":
        return SlashResult(handled=True, action="login")
    if cmd == "/logout":
        return SlashResult(handled=True, action="logout")
    if cmd == "/clear":
        return SlashResult(handled=True, action="clear", message="Screen cleared, counters reset.")
    if cmd in ("/cost", "/usage"):
        return SlashResult(handled=True, action="cost",
                           message=f"This session: {ctx.tokens} tokens (~${ctx.cost_usd:.4f}). "
                                   f"LLM turns don't spend credits.")
    if cmd == "/status":
        auth = "signed in" if ctx.logged_in else "not signed in (/login)"
        msg = (f"surface: {ctx.surface}   mode: {ctx.mode}   {auth}\n"
               f"cwd: {ctx.workspace}   git: {ctx.git_branch}\n"
               f"tokens: {ctx.tokens} (~${ctx.cost_usd:.4f})   webbee v{ctx.version}")
        return SlashResult(handled=True, action="status", message=msg)
    if cmd == "/mode":
        if not args:
            return SlashResult(handled=True, action="mode", new_mode=None,
                               message=f"Current mode: {ctx.mode}. Available: {', '.join(_MODES)}.")
        want = args[0].lower()
        if want not in _MODES:
            return SlashResult(handled=True, action="mode", new_mode=None,
                               message=f"Unknown mode '{want}'. Available: {', '.join(_MODES)}.")
        return SlashResult(handled=True, action="mode", new_mode=want,
                           message=f"Mode → {want}.")
    return SlashResult(handled=True, message=f"Unknown command '{cmd}'. /help for the list.")
