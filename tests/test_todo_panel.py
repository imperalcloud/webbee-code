"""The STICKY todo panel's PURE builders (webbee.todo_panel, 0.3.15) — the
dock pins the current checklist above the queue panel and re-reads the
sink-owned list every redraw; these cover the fragments/height contract:
glyph-per-status rows, derived (done/total) header, the completed-collapse +
overflow caps (the ▶ current item is never hidden), and the empty/malformed
degradations that keep the panel from ever crashing a redraw."""
import re

from webbee.todo_panel import TP_MAX_ITEMS, todo_fragments, todo_height

NO_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def _t(content, status="pending"):
    return {"content": content, "status": status}


def _text(frags):
    return "".join(f[1] for f in frags)


def test_empty_or_malformed_only_list_renders_no_panel():
    assert todo_fragments([]) == [] and todo_height([]) == 0
    assert todo_fragments(None) == [] and todo_height(None) == 0
    junk = ["garbage", None, {"status": "pending"}, {"content": "   "}]
    assert todo_fragments(junk) == [] and todo_height(junk) == 0


def test_header_counts_and_status_glyph_rows_in_plan_order():
    frags = todo_fragments([_t("map the repo", "completed"),
                            _t("fix the bug", "in_progress"),
                            _t("run tests")])
    text = _text(frags)
    assert "📋 Todos (1/3)" in text                     # done/total derived
    assert "✓ " in text and "▶ fix the bug" in text and "○ run tests" in text
    assert text.index("map the repo") < text.index("fix the bug") < text.index("run tests")
    assert not NO_CYRILLIC.search(text)                 # English UI only


def test_row_styles_completed_struck_current_accented_pending_muted():
    frags = todo_fragments([_t("done thing", "completed"),
                            _t("now thing", "in_progress"),
                            _t("later thing", "weird-status")])
    styles = {f[1].strip(): f[0] for f in frags}
    assert styles["done thing"] == "class:tp.done.text"     # dim + struck text
    assert [f[0] for f in frags if "✓" in f[1]] == ["class:tp.done"]
    assert styles["▶ now thing"] == "class:tp.now"          # accent — pops
    assert styles["○ later thing"] == "class:tp.item"       # unknown → pending row


def test_rows_are_one_line_and_truncated_to_width():
    frags = todo_fragments([_t("x " * 300, "in_progress")], width=40)
    row = next(f[1] for f in frags if "▶" in f[1])
    line = row.lstrip("\n")
    assert "\n" not in line and line.endswith("…") and len(line) <= 40


def test_completed_collapse_first_when_over_the_cap():
    items = [_t(f"old {i}", "completed") for i in range(TP_MAX_ITEMS)] + \
            [_t("current", "in_progress"), _t("next up")]
    text = _text(todo_fragments(items))
    assert f"… ✓{TP_MAX_ITEMS} done" in text            # history compresses first
    assert "old 0" not in text                          # collapsed rows are gone
    assert "▶ current" in text and "○ next up" in text  # the live plan keeps its rows
    assert f"({TP_MAX_ITEMS}/{TP_MAX_ITEMS + 2})" in text   # header keeps full truth


def test_pending_overflow_caps_with_more_row_and_current_never_hides():
    # A huge plan whose ▶ item sits BEYOND the cap: the cap keeps the panel
    # bounded ("+K more") but the current item replaces the last shown row.
    items = [_t(f"p{i}") for i in range(TP_MAX_ITEMS + 3)]
    items.append(_t("the current one", "in_progress"))
    frags = todo_fragments(items)
    text = _text(frags)
    assert "▶ the current one" in text                  # never hidden by the cap
    assert f"… +{len(items) - TP_MAX_ITEMS} more" in text
    shown_rows = [f for f in frags if f[1].startswith("\n   ") and "…" not in f[1]]
    assert len(shown_rows) == TP_MAX_ITEMS              # row budget holds


def test_height_always_matches_the_rendered_lines():
    for items in ([_t("a", "completed"), _t("b", "in_progress"), _t("c")],
                  [_t(f"p{i}") for i in range(TP_MAX_ITEMS + 5)],
                  [_t(f"d{i}", "completed") for i in range(TP_MAX_ITEMS + 2)],
                  [_t("only", "in_progress")]):
        frags = todo_fragments(items)
        assert todo_height(items) == _text(frags).count("\n") + 1


# ── W1 front-3b task 11: click-to-collapse (header toggles to one row) ──────

def test_todo_collapsed_single_row_and_height():
    todos = [_t("x", "in_progress"), _t("y")]
    frags = todo_fragments(todos, collapsed=True, toggle=lambda: None)
    assert len(frags) == 1 and "Todos (0/2)" in frags[0][1] and "▸" in frags[0][1]
    assert len(frags[0]) == 3                      # header carries the toggle handler
    assert todo_height(todos, collapsed=True) == 1


def test_todo_header_toggle_fires_on_mouse_up():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    hits = []
    frags = todo_fragments([_t("a")], collapsed=False, toggle=lambda: hits.append(1))
    handler = frags[0][2]
    up = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(up) is None and hits == [1]
    scroll = MouseEvent(position=Point(0, 0), event_type=MouseEventType.SCROLL_UP,
                        button=MouseButton.LEFT, modifiers=frozenset())
    assert handler(scroll) is NotImplemented
