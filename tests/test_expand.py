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
    assert pane.toggle_at(line)
    opened = _rows(pane)
    text = "\n".join(opened)
    assert "$ " + CMD in text and "timeout: 120" in text
    for part in ("tests/test_a.py ....F", "FAILED tests/test_a.py::test_x", "1 failed, 4 passed", "[exit 1]"):
        assert part in text
    # the block stands between its own line and the next one: nothing after it moved out of order
    assert _at(pane, "click the action to close") == line + 2
    assert _at(pane, "a line after it") > _at(pane, "[exit 1]")
    assert pane.toggle_at(line)
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
    assert pane.toggle_at(_at(pane, "more lines"))
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


# ── an action the SERVER ran: its record is asked for when the line is first opened ──
def _server_pane(fetch):
    from webbee.frames import handle_step_finished, handle_step_started
    pane = OutputPane(width=92)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    sink.begin_turn()
    started, finished, labels, local_ids, steps, args = set(), set(), {}, set(), [], {}
    handle_step_started({"type": "step_started", "step_id": "call_7", "kind": "tool", "app_id": "wordpress-hub",
                         "tool": "update_post"}, sink, started, labels, local_ids, args)
    handle_step_finished({"type": "step_finished", "step_id": "call_7", "ok": True, "duration_ms": 900,
                          "summary_facts": {"count": 1}}, sink, finished, labels, steps, local_ids, args, fetch)
    return pane, steps


def test_a_server_run_action_opens_with_the_servers_record_of_it():
    import asyncio
    import json
    asked = []

    async def fetch(step_id):
        asked.append(step_id)
        return {"ok": True, "duration_ms": 900,
                "args": {"preview": json.dumps({"id": 2248, "title": "cPanel alternatives"}), "truncated": False,
                         "total_bytes": 40},
                "result": {"preview": json.dumps({"ok": True, "link": "https://example.org/p/2248"}),
                           "truncated": True, "total_bytes": 31368}}
    pane, steps = _server_pane(fetch)
    closed = _rows(pane)
    assert steps == [{"step_id": "call_7", "label": "wordpress-hub·update_post", "ok": True}]
    line = _at(pane, "update_post")
    record = pane.toggle_at(line)
    assert record and record.remote.state == "new" and asked == []
    assert "asking the server" in "\n".join(_rows(pane))                 # open at once, the record on its way
    assert asyncio.run(record.remote.load()) is True and asked == ["call_7"]
    pane.rerender()
    text = "\n".join(_rows(pane))
    assert "asking the server" not in text
    assert '"id": 2248' in text and '"title": "cPanel alternatives"' in text
    assert '"link": "https://example.org/p/2248"' in text
    assert "the server keeps the first part of 31368 bytes" in text      # said, not hidden
    assert asyncio.run(record.remote.load()) is False and asked == ["call_7"]      # asked once
    pane.toggle_at(line)
    assert _rows(pane) == closed


def test_a_server_run_action_with_no_record_says_so():
    import asyncio

    async def fetch(step_id):
        return {}
    pane, _ = _server_pane(fetch)
    record = pane.toggle_at(_at(pane, "update_post"))
    asyncio.run(record.remote.load())
    pane.rerender()
    assert "the server holds no record of this action" in "\n".join(_rows(pane))

    async def boom(step_id):
        raise RuntimeError("offline")
    pane, _ = _server_pane(boom)
    record = pane.toggle_at(_at(pane, "update_post"))
    assert asyncio.run(record.remote.load()) is True and record.remote.detail == {}


def test_a_connections_command_shows_what_was_asked_before_the_record_comes():
    from webbee.frames import handle_step_finished, handle_step_started

    async def fetch(step_id):
        return {}
    pane = OutputPane(width=92)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    started, finished, labels, local_ids, steps, args = set(), set(), {}, set(), [], {}
    handle_step_started({"step_id": "c1", "kind": "external", "app_id": "conn-ssh", "tool": "run_command",
                         "args": {"command": "systemctl status nginx --no-pager", "connection_id": "abc"}},
                        sink, started, labels, local_ids, args)
    handle_step_finished({"step_id": "c1", "ok": True, "duration_ms": 10, "summary_facts": {}},
                         sink, finished, labels, steps, local_ids, args, fetch)
    assert args == {}                                           # handed to the line, not kept twice
    pane.toggle_at(_at(pane, "run_command"))
    assert "command: systemctl status nginx --no-pager" in "\n".join(_rows(pane))


def test_without_a_way_to_ask_a_server_step_is_the_plain_line_it_was():
    pane, _ = _server_pane(None)
    assert pane.toggle_at(_at(pane, "update_post")) is False
    assert not any("click for all" in ln for ln in _rows(pane))


# ── copying: the text, not the layout (the owner 2026-11-26: "без лишних символов и пробелов") ──
ANSWER = "Готово.\n\n- первый пункт\n- второй пункт\n\n```bash\nsystemctl restart nginx\n```\n"


def _answer_pane():
    pane = OutputPane(width=60)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    sink.begin_turn()
    sink.end_turn(ANSWER)
    return pane


def test_a_click_on_an_answers_head_copies_the_answer_as_it_was_written(monkeypatch):
    import webbee.clipboard as cb
    pane = _answer_pane()
    got = []
    monkeypatch.setattr(cb, "copy_to_clipboard", lambda t: got.append(t) or "copied")
    before = _rows(pane)
    assert pane.toggle_at(_at(pane, "🐝 Webbee"))
    assert got == [ANSWER.strip()]                     # the markdown itself: no bullets drawn, no wrap, no padding
    assert _rows(pane) == before and pane.copy_flash == "copied"


def test_a_drag_over_an_answer_copies_no_gutter_and_no_trailing_spaces(monkeypatch):
    import webbee.clipboard as cb
    pane = _answer_pane()
    got = []
    monkeypatch.setattr(cb, "copy_to_clipboard", lambda t: got.append(t) or "copied")
    a, b = _at(pane, "Готово."), _at(pane, "systemctl restart nginx")
    pane._copy_selection((a, 0), (b, 400))
    (text,) = got
    assert text.startswith("Готово.") and "systemctl restart nginx" in text
    assert all(ln == ln.rstrip() for ln in text.split("\n"))        # nothing drawn to the right of a line
    assert not text.endswith("\n") and not text.startswith("\n")
    pane._copy_selection((a, 0), (a, 400))                            # one line: only its words
    assert got[-1] == "Готово."


def test_tidy_keeps_the_indent_that_belongs_to_the_text():
    tidy = OutputPane._tidy
    assert tidy("    def f():   \n        return 1   \n") == "def f():\n    return 1"
    assert tidy("def f():\n        return 1", first_col=4) == "def f():\n    return 1"     # the drag began at the "d"
    assert tidy("f():\n        return 1", first_col=8) == "f():\nreturn 1"                # began inside the line
    assert tidy("ine10\nline11\nli", first_col=1) == "ine10\nline11\nli"                  # a piece is the piece
    assert tidy("  word  ") == "word" and tidy("a  b") == "a  b"
    assert tidy("  one  ") == "one"


def test_a_click_on_an_opened_blocks_head_copies_the_whole_action_as_plain_text(monkeypatch):
    import webbee.clipboard as cb
    pane, _ = _pane()
    got = []
    monkeypatch.setattr(cb, "copy_to_clipboard", lambda t: got.append(t) or "copied")
    line = _at(pane, "Shell Exec")
    pane.toggle_at(line)
    opened = _rows(pane)
    assert pane.toggle_at(_at(pane, "click here to copy all")) and _rows(pane) == opened      # copied, still open
    (text,) = got
    assert text.startswith("$ " + CMD + "\ntimeout: 120\n\ntests/test_a.py ....F\n")
    assert text.endswith("[exit 1]") and "▾" not in text and "─" not in text
    assert ("long " * 40).strip() in text                              # a long line is ONE line, not as wrapped
    pane.toggle_at(_at(pane, "FAILED tests"))                           # anywhere else in the block: it closes
    assert len(got) == 1 and "click here to copy all" not in "\n".join(_rows(pane))


def test_a_click_on_an_opened_diffs_head_copies_the_whole_diff(monkeypatch):
    import webbee.clipboard as cb
    pane = OutputPane(width=92)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    diff = "\n".join(["--- a/x", "+++ b/x"] + [f"+line {i}" for i in range(100)])
    sink.tool_diff("edit_file", diff)
    got = []
    monkeypatch.setattr(cb, "copy_to_clipboard", lambda t: got.append(t) or "copied")
    pane.toggle_at(_at(pane, "more lines"))
    pane.toggle_at(_at(pane, "copy the whole diff"))
    assert got == [diff]


# ── opening and closing never move the page (the owner, live 2026-11-26) ──
def _long_page():
    pane = OutputPane(width=92)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    sink.begin_turn()
    sink.end_turn("\n\n".join(f"Абзац ответа номер {i}." for i in range(40)))       # a long answer: one record
    sink.begin_turn()
    sink.tool_start("bash", {"command": CMD})
    sink.tool_result("bash", False, "tests/test_a.py ....F",
                     detail={"args": {"command": CMD}, "result": {"ok": False, "content": OUT}})
    sink.end_turn("\n\n".join(f"Второй ответ, абзац {i}." for i in range(40)))
    pane._view_h = 12
    return pane


def test_a_click_keeps_the_clicked_line_on_its_screen_row_while_a_long_answer_stands_half_scrolled_above():
    pane = _long_page()
    line = _at(pane, "Shell Exec")
    pane._follow, pane._offset = False, line - 7          # the action 7 rows down; above it the tail of answer 1
    top_before = _rows(pane)[pane._offset]
    assert "Абзац ответа" in "\n".join(_rows(pane)[pane._offset:line]) or top_before == ""
    pane.toggle_at(line)
    assert _rows(pane)[pane._offset + 7].strip().startswith("1. ")      # the action is where it was on the screen
    assert _rows(pane)[pane._offset] == top_before                        # and so is everything above it
    opened_offset = pane._offset
    pane.toggle_at(_at(pane, "Shell Exec"))                               # close: back, nothing moved
    assert pane._offset == opened_offset and _rows(pane)[pane._offset + 7].strip().startswith("1. ")


def test_a_click_while_the_page_follows_the_tail_does_not_jump_either():
    pane = _long_page()
    pane.notify()                                          # at the tail, following
    assert pane._follow
    line = _at(pane, "Второй ответ, абзац 39.")
    screen_row = line - pane._offset
    target = _at(pane, "Shell Exec")
    pane._offset = target - 3                              # the reader scrolled up to the action; follow flag stale
    pane.toggle_at(target)
    assert _rows(pane)[pane._offset + 3].strip().startswith("1. ")
    assert screen_row >= 0


def test_a_record_that_changes_above_the_view_does_not_move_what_is_read():
    pane = _long_page()
    reading = _at(pane, "Второй ответ, абзац 20.")
    pane._follow, pane._offset = False, reading
    record = next(o for objs, _ in pane._records for o in objs if hasattr(o, "toggle"))
    record.toggle()                                        # an action far above opens (its detail arrived)
    pane.rerender()
    assert "Второй ответ, абзац 20." in _rows(pane)[pane._offset]


# ── a step's notes line opens whole; steps stand apart (the owner, live 2026-11-26) ──
LONG_EXPECT = "exit 0 and the list of every file under src/webbee that still names the old toolbar hint, " * 3


def _thought_pane():
    pane = OutputPane(width=80)
    sink = RichSink(pane.console, live_enabled=False, on_output=pane.notify)
    sink.begin_turn()
    sink.thinking("x", say="Проверяю подсказки.", kind="step",
                  lines={"learned": "the hint is built in one place", "expect": LONG_EXPECT.strip()})
    sink.tool_start("bash", {"command": "grep -rn Tip src"})
    sink.tool_result("bash", True, "src/webbee/tui.py", detail={"args": {"command": "grep -rn Tip src"},
                                                                 "result": {"content": "src/webbee/tui.py"}})
    sink.thinking("y", say="Теперь правлю.", kind="step", lines={"expect": "exit 0"})
    return pane


def test_the_notes_line_is_cut_to_the_width_and_a_click_opens_it_whole(monkeypatch):
    import webbee.clipboard as cb
    pane = _thought_pane()
    closed = _rows(pane)
    assert all(len(ln) <= 80 for ln in closed)                       # nothing past the right edge
    line = _at(pane, "learned: the hint is built in one place")
    assert closed[line].endswith("…") and "still names the old toolbar hint" not in "\n".join(closed)
    assert pane.toggle_at(line)
    whole = " ".join(ln.strip() for ln in _rows(pane))                # a long value wraps: read it as one text
    assert whole.count("still names the old toolbar hint") == 3 and all(len(ln) <= 80 for ln in _rows(pane))
    opened = _rows(pane)
    a = _at(pane, "▾ notes")
    assert opened[a + 1].strip().startswith("learned: ") and any(r.strip().startswith("expect: ") for r in opened)
    got = []
    monkeypatch.setattr(cb, "copy_to_clipboard", lambda t: got.append(t) or "copied")
    pane.toggle_at(a)
    assert got == ["learned: the hint is built in one place\nexpect: " + LONG_EXPECT.strip()]
    pane.toggle_at(line)
    assert _rows(pane) == closed


def test_a_step_stands_apart_from_the_one_before_it_by_one_empty_line():
    pane = _thought_pane()
    rows = _rows(pane)
    first, second = _at(pane, "Проверяю подсказки."), _at(pane, "Теперь правлю.")
    assert rows[:first].count("") == first and first <= 2            # only the turn's own gap above the first
    assert rows[second - 1] == "" and rows[second - 2].strip() != ""  # one empty line above the next step
