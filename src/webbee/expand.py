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

    def __init__(self, closed, opened=None):
        self.closed = closed
        self._opened = opened
        self.open = False
        if opened is not None:
            _held.append(self)
            while len(_held) > _HELD_MAX:
                _held.popleft()._opened = _gone

    def toggle(self) -> bool:
        self.open = not self.open
        return self.open

    def __rich_console__(self, console, options):
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
    return _block(f"▾ {clean(tool)} · {n} line{'' if n == 1 else 's'} of output · click the line again to close",
                  parts, 3)


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
    return _block(f"▾ the other {len(lines)} lines of the diff · click the diff again to close", [body], 0)
