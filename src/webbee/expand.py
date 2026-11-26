"""A transcript record that a click opens and closes in place.

The owner, 2026-11-26: under her comments she runs commands, reads and writes -- and each of it was one cut line,
two at most; only a diff was seen, and not whole. A click on an action's line now opens what the client itself
holds of it: the whole command or arguments and the whole result, as plain lines with no frame around them, so a
drag copies exactly the text. A second click closes it. The record carries its own detail -- no table of line
numbers beside the transcript to keep in step with it."""
from collections import deque

from rich.text import Text

_HELD_MAX = 300                 # actions whose whole detail stays in memory; an older one says it is gone
_held = deque()


class Expandable:
    """``closed`` is what the record always shows; ``opened`` builds what a click adds under it."""

    def __init__(self, closed, opened=None, whole=None):
        self.closed = closed
        self._opened = opened
        self._whole = whole          # () -> the block's own text, for a click on the opened block's head line
        self._closed_rows = 0        # lines the closed part took when it was last drawn
        self.open = False
        if opened is not None:
            _held.append(self)
            while len(_held) > _HELD_MAX:
                _held.popleft()._opened = _gone

    def toggle(self) -> bool:
        self.open = not self.open
        return self.open

    def copy_text(self, row: int) -> str:
        """The block's whole text when the click is on the opened block's HEAD line (the first line under the
        action's own); "" anywhere else -- there a click opens or closes."""
        if self.open and self._whole is not None and self._closed_rows and row == self._closed_rows:
            try:
                return str(self._whole() or "")
            except Exception:
                return ""
        return ""

    def __rich_console__(self, console, options):
        self._closed_rows = len(console.render_lines(self.closed, options, pad=False))
        yield self.closed
        if self.open and self._opened is not None:
            yield self._opened()


_RULE = "─" * 44


def _block(head: str, parts, left: int):
    """One opened block on one grid: a head line, the parts each kept at the same left edge -- a line too long for
    the terminal wraps UNDER its own start, never back to the screen's edge -- and a closing rule, so where the
    block begins and ends is plain however long it is."""
    from rich.console import Group
    from rich.padding import Padding
    rows = [Padding(Text(head, style="dim grey50"), (0, 0, 0, left))]
    rows += [Padding(p, (0, 0, 0, left + 2)) for p in parts]
    rows.append(Padding(Text(_RULE, style="dim grey50"), (0, 0, 0, left)))
    return Group(*rows)


class Copyable:
    """A transcript record a click copies WHOLE, as it was written: the head of an answer carries the answer's
    own text. A drag over a drawn answer can only copy what the terminal shows (wrapped lines, a list's drawn
    bullets, a table's frame); the click gives the text itself, with nothing added."""

    def __init__(self, shown, copy_text: str):
        self.shown = shown
        self.copy_text = copy_text

    def __rich_console__(self, console, options):
        yield self.shown


def _gone():
    return _block("▾ the whole detail of this action is no longer held "
                  f"(it is older than the last {_HELD_MAX} actions)", [], 3)


def _text(text: str, clean) -> str:
    return clean(text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")).rstrip("\n")


def step_body(tool: str, args, result, clean):
    """The whole of one local action: what was asked (a command as it was written, other arguments by name) and
    all the client holds of the result. ``clean`` strips terminal escapes -- the content is a tool's, untrusted;
    a tab becomes four spaces, so columns stand where the tool put them."""
    a = args if isinstance(args, dict) else {}
    parts = []
    command = a.get("command")
    if isinstance(command, str) and command:
        parts.append(Text("$ " + _text(command, clean), style="cyan"))
    asked = Text()
    for key in sorted(k for k in a if k != "command"):
        value = a[key]
        shown = _text(value if isinstance(value, str) else repr(value), clean)
        if asked.plain:
            asked.append("\n")
        asked.append(f"{clean(key)}: ", style="dim")
        asked.append(("\n" + shown) if "\n" in shown else shown)
    if asked.plain:
        parts.append(asked)
    r = result if isinstance(result, dict) else {}
    content = _text(str(r.get("content") or ""), clean)
    if parts:
        parts.append(Text(""))                 # what was asked and what came back stand apart
    parts.append(Text(content) if content else Text("(no output)", style="dim"))
    n = content.count("\n") + 1 if content else 0
    return _block(f"▾ {clean(tool)} · {n} line{'' if n == 1 else 's'} · ⧉ click here to copy all · "
                  "click the action to close", parts, 3)


def protocol_body(lines: dict):
    """A step's protocol (learned / expect / need) opened whole: each label on its own line, its value beside it
    and wrapped under its own start. The values come cleaned by the caller."""
    body = Text()
    for i, (key, value) in enumerate(lines.items()):
        if i:
            body.append("\n")
        body.append(f"{key}: ", style="dim")
        body.append(str(value))
    return _block("▾ notes · ⧉ click here to copy · click above to close", [body], 9)


def step_text(args, result, clean) -> str:
    """The same action as plain text, for the clipboard: the command (or the arguments), an empty line, the
    result -- nothing drawn, nothing indented."""
    a = args if isinstance(args, dict) else {}
    head = []
    if isinstance(a.get("command"), str) and a["command"]:
        head.append("$ " + _text(a["command"], clean))
    for key in sorted(k for k in a if k != "command"):
        value = a[key]
        head.append(f"{clean(key)}: " + _text(value if isinstance(value, str) else repr(value), clean))
    r = result if isinstance(result, dict) else {}
    content = _text(str(r.get("content") or ""), clean)
    return "\n".join(head) + ("\n\n" if head and content else "") + content


def diff_rest(lines, clean):
    """The lines of a diff past the ones always shown, in a diff's own colours, at the diff's own left edge."""
    body = Text()
    for i, line in enumerate(lines):
        if i:
            body.append("\n")
        style = ("green" if line.startswith("+") and not line.startswith("+++") else
                 "red" if line.startswith("-") and not line.startswith("---") else
                 "cyan dim" if line.startswith("@@") else "dim")
        body.append(_text(line, clean), style=style)
    return _block(f"▾ {len(lines)} more lines · ⧉ click here to copy the whole diff · click the diff to close",
                  [body], 0)


def _shown(preview) -> str:
    """A stored preview (JSON text of a value) as a person reads it: a text as itself, a structure indented."""
    import json
    raw = str(preview or "")
    try:
        value = json.loads(raw)
    except Exception:
        return raw                                   # a preview cut mid-value is shown as it was kept
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


class RemoteStep:
    """The detail of an action the SERVER ran (an app's tool, a connection's command): the client holds only its
    line, the kernel keeps the record (bounded previews, the owner's own, 24 hours). It is asked for when the
    line is first opened; until it comes the block says so, and what the step's start frame told (the arguments
    of a connection's command) is shown at once."""

    def __init__(self, tool: str, args, fetch, clean):
        self.tool, self.args, self._fetch, self._clean = tool, args if isinstance(args, dict) else {}, fetch, clean
        self.state = "new"                           # new -> loading -> done
        self.detail: dict = {}

    async def load(self) -> bool:
        """Fetch the record once. True: the block has something new to show."""
        if self.state != "new":
            return False
        self.state = "loading"
        try:
            got = await self._fetch()
        except Exception:
            got = {}
        self.detail = got if isinstance(got, dict) else {}
        self.state = "done"
        return True

    def _part(self, name: str, field) -> list:
        f = field if isinstance(field, dict) else {}
        text = _text(_shown(f.get("preview")), self._clean)
        if not text or text in ("{}", "null"):
            return []
        head = Text(f"{name}:", style="dim")
        if f.get("truncated"):
            head.append(f"  (the server keeps the first part of {int(f.get('total_bytes') or 0)} bytes)",
                        style="dim italic")
        return [head, Text(text)]

    def body(self):
        clean, tool = self._clean, self._clean(self.tool)
        asked = Text()
        for key in sorted(self.args):
            value = self.args[key]
            shown = _text(value if isinstance(value, str) else repr(value), clean)
            if asked.plain:
                asked.append("\n")
            asked.append(f"{clean(key)}: ", style="dim")
            asked.append(("\n" + shown) if "\n" in shown else shown)
        known = [asked] if asked.plain else []
        if self.state != "done":
            return _block(f"▾ {tool} · asking the server for the whole of this action…", known, 3)
        d = self.detail
        if not d:
            return _block(f"▾ {tool} · the server holds no record of this action (it keeps one for 24 hours) · "
                          "click the action to close", known, 3)
        parts = self._part("arguments", d.get("args")) or known
        result = self._part("result", d.get("result"))
        if parts and result:
            parts = parts + [Text("")]
        seconds = (d.get("duration_ms") or 0) / 1000
        return _block(f"▾ {tool} · {seconds:.1f}s · ⧉ click here to copy all · click the action to close",
                      parts + (result or [Text("(no result kept)", style="dim")]), 3)

    def text(self) -> str:
        """The record as plain text, for the clipboard ("" until it has come)."""
        d = self.detail if self.state == "done" else {}
        out = []
        for name in ("args", "result"):
            f = d.get(name) if isinstance(d.get(name), dict) else {}
            shown = _text(_shown(f.get("preview")), self._clean)
            if shown and shown not in ("{}", "null"):
                out.append(("arguments" if name == "args" else "result") + ":\n" + shown)
        return "\n\n".join(out)
