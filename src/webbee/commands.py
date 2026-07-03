from dataclasses import dataclass

_MODES = ("default", "plan", "autopilot")

_HELP = """Команды:
  /help              эта справка
  /login             вход в аккаунт Imperal (браузер)
  /logout            выйти, удалить локальные креды
  /clear             очистить экран + сбросить счётчики сессии
  /mode [default|plan|autopilot]   режим согласия (без арг — показать текущий)
  /cost  (=/usage)   токены + $-стоимость за сессию
  /status            cwd · git · поверхность · тир · баланс · версия
  /exit  (=/quit)    выход"""


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
        return SlashResult(handled=True, action="clear", message="Экран очищен, счётчики сброшены.")
    if cmd in ("/cost", "/usage"):
        return SlashResult(handled=True, action="cost",
                           message=f"За сессию: {ctx.tokens} tokens (~${ctx.cost_usd:.4f}). "
                                   f"LLM-ходы не списывают credits.")
    if cmd == "/status":
        auth = "вошёл" if ctx.logged_in else "НЕ вошёл (/login)"
        msg = (f"поверхность: {ctx.surface}   режим: {ctx.mode}   {auth}\n"
               f"cwd: {ctx.workspace}   git: {ctx.git_branch}\n"
               f"tokens: {ctx.tokens} (~${ctx.cost_usd:.4f})   webbee v{ctx.version}")
        return SlashResult(handled=True, action="status", message=msg)
    if cmd == "/mode":
        if not args:
            return SlashResult(handled=True, action="mode", new_mode=None,
                               message=f"Текущий режим: {ctx.mode}. Доступно: {', '.join(_MODES)}.")
        want = args[0].lower()
        if want not in _MODES:
            return SlashResult(handled=True, action="mode", new_mode=None,
                               message=f"Неизвестный режим «{want}». Доступно: {', '.join(_MODES)}.")
        return SlashResult(handled=True, action="mode", new_mode=want,
                           message=f"Режим → {want}.")
    return SlashResult(handled=True, message=f"Неизвестная команда «{cmd}». /help — список.")
