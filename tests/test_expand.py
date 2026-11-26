"""A click on an action's line opens the whole of it in place; a second click closes it; a drag copies the text.

The owner, 2026-11-26: under her comments each action was one cut line, two at most, and only a diff was seen (not
whole). He asked: a click opens it fully, it opens and closes neatly, the text stands on one grid, nothing slides,
and it copies without trouble."""
from types import SimpleNamespace

from webbee.output_pane import OutputPane
from webbee.render import RichSink

CMD = "cd /srv/app && pytest -q tests/test_a.py 2>&1 | tail -5"
OUT = ("tests/test_a.py ....F\n\tindented\n" + "long " * 40 + "\nFAILED tests/test_a.py::test_x\n"
       "1 failed, 4 passed\n[exit 1]")


def _pane():
    pane = OutputPane(width=92)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    sink.begin_turn()
    sink.tool_start("bash", {"command": CMD})
    sink.tool_result("bash", False, "tests/test_a.py ....F",
                     detail={"args": {"command": CMD, "timeout": 120}, "result": {"ok": False, "content": OUT}})
    sink.note("a line after it")
    return pane, sink


def _rows(pane):
    return [ln.rstrip() for ln in pane._plain_lines()]


def _at(pane, needle):
    return next(i for i, ln in enumerate(_rows(pane)) if needle in ln)


def test_a_click_opens_the_whole_action_under_its_line_and_a_second_click_puts_everything_back():
    pane, _ = _pane()
    closed = _rows(pane)
    assert CMD not in "\n".join(closed)                        # the line shows the command cut
    assert any("▸ click for all" in ln for ln in closed)
    line = _at(pane, "Shell Exec")
    assert pane.toggle_at(line) is True
    opened = _rows(pane)
    text = "\n".join(opened)
    assert "$ " + CMD in text and "timeout: 120" in text
    for part in ("tests/test_a.py ....F", "FAILED tests/test_a.py::test_x", "1 failed, 4 passed", "[exit 1]"):
        assert part in text
    # the block stands between its own line and the next one: nothing after it moved out of order
    assert _at(pane, "click the line again to close") == line + 2
    assert _at(pane, "a line after it") > _at(pane, "[exit 1]")
    assert pane.toggle_at(line) is True
    assert _rows(pane) == closed                                # closed again: line for line as it was


def test_the_opened_text_stands_on_one_grid_and_a_long_line_wraps_under_its_own_start():
    pane, _ = _pane()
    pane.toggle_at(_at(pane, "Shell Exec"))
    rows = _rows(pane)
    a, b = _at(pane, "$ " + CMD[:10]), _at(pane, "[exit 1]")
    body = [ln for ln in rows[a:b + 1] if ln]
    edge = len(rows[a]) - len(rows[a].lstrip())
    assert all(len(ln) - len(ln.lstrip()) >= edge for ln in body)        # no line starts left of the block
    wrapped = [ln for ln in body if ln.strip().startswith("long long")]
    assert len(wrapped) >= 2 and len({len(ln) - len(ln.lstrip()) for ln in wrapped}) == 1
    assert all(len(ln) <= 92 for ln in rows)                             # nothing runs past the terminal's edge
    assert not any("\t" in ln for ln in rows)                            # a tab is spaces: columns stay put


def test_a_drag_over_the_opened_output_copies_the_text_without_the_layout(monkeypatch):
    import webbee.clipboard as cb
    pane, _ = _pane()
    pane.toggle_at(_at(pane, "Shell Exec"))
    got = []
    monkeypatch.setattr(cb, "copy_to_clipboard", lambda t: got.append(t) or "copied")
    pane._copy_selection((_at(pane, "FAILED tests"), 0), (_at(pane, "[exit 1]"), 200))
    assert got == ["FAILED tests/test_a.py::test_x\n1 failed, 4 passed\n[exit 1]"]


def test_a_click_where_nothing_opens_changes_nothing():
    pane, _ = _pane()
    before = _rows(pane)
    assert pane.toggle_at(_at(pane, "a line after it")) is False
    assert pane.toggle_at(10 ** 6) is False or _rows(pane) == before
    assert _rows(pane) == before


def test_the_view_stays_on_the_clicked_action_when_the_reader_scrolled_up():
    pane, sink = _pane()
    for i in range(80):
        sink.note(f"later line {i}")
    pane._view_h = 10
    line = _at(pane, "Shell Exec")
    pane._follow, pane._offset = False, line
    pane.toggle_at(line)
    assert _rows(pane)[pane._offset].strip().startswith("1. ") and pane._follow is False


def test_a_long_diff_shows_its_head_and_a_click_opens_the_rest():
    pane = OutputPane(width=92)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    sink.tool_diff("edit_file", "\n".join(["--- a/x", "+++ b/x"] + [f"+line {i}" for i in range(100)]))
    text = "\n".join(_rows(pane))
    assert "+line 0" in text and "+line 99" not in text and "more lines" in text
    assert pane.toggle_at(_at(pane, "more lines")) is True
    text = "\n".join(_rows(pane))
    assert "+line 99" in text and text.count("+line 50") == 1
    short = OutputPane(width=92)
    RichSink(short.console, live_enabled=False).tool_diff("edit_file", "+a\n+b")
    assert short.toggle_at(1) is False                            # a short diff is whole already


def test_untrusted_output_cannot_carry_escapes_into_the_opened_block():
    pane = OutputPane(width=92)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    sink.tool_start("bash", {"command": "x"})
    sink.tool_result("bash", True, "ok", detail={"args": {"command": "x\x1b[?1003h"},
                                                  "result": {"content": "a\x1b]0;title\x07b\x1b[31mc"}})
    pane.toggle_at(_at(pane, "Shell Exec"))
    raw = pane.dump()
    assert "\x1b[?1003h" not in raw and "\x1b]0;" not in raw and "\x07" not in raw


def test_only_the_newest_actions_keep_their_whole_detail():
    import webbee.expand as ex
    first = ex.Expandable("first", lambda: "detail")
    for _ in range(ex._HELD_MAX):
        ex.Expandable("x", lambda: "d")
    assert first._opened is ex._gone                              # the oldest says its detail is gone


def test_a_sink_that_takes_no_detail_gets_the_line_as_before():
    from webbee.frames import show_tool_result
    seen = []
    old = SimpleNamespace(tool_result=lambda tool, ok, summary: seen.append((tool, ok, summary)))
    show_tool_result(old, "bash", True, "done", {"command": "x"}, {"content": "y"})
    assert seen == [("bash", True, "done")]
