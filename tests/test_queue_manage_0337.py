"""0.3.37 — managing a MULTI-ITEM queue.

Before this, the only way out of the type-ahead queue was `pull_item`, bound
to ↑, which (a) hijacks the input buffer and (b) only ever reached the NEWEST
row. So a 3-deep queue could not have its 1st or 2nd item removed at all.

Two new paths, one shared implementation:
  * per-row ✕ in the panel        -> queue_panel.drop_item (mouse)
  * /queue drop N | /queue edit N -> the same drop_item / pull_item (keyboard,
    for terminals where mouse reporting is off or unavailable)
"""
from collections import deque

from webbee.commands import CommandContext, dispatch
from webbee.queue_panel import drop_item, one_line, pull_item, queue_fragments


class _Buf:
    def __init__(self, text=""):
        self.text = text
        self.cursor_position = len(text)


def _ctx(queued):
    return CommandContext(mode="default", workspace="/w", version="0.3.37",
                          surface="terminal", logged_in=True, session_tokens=0,
                          session_credits=0, git_branch="main",
                          queued=tuple(queued))


# ── drop_item: the remove primitive ───────────────────────────────────────
def test_drop_removes_the_middle_item_without_touching_the_buffer():
    pending = deque(["first", "second", "third"])
    buf = _Buf("half-typed thought")
    removed = drop_item(pending, 1)
    assert removed == "second"
    assert list(pending) == ["first", "third"]
    assert buf.text == "half-typed thought"   # a removal must not hijack typing


def test_drop_can_remove_the_very_first_item():
    pending = deque(["first", "second", "third"])
    assert drop_item(pending, 0) == "first"
    assert list(pending) == ["second", "third"]


def test_drop_ignores_a_stale_index_instead_of_raising():
    pending = deque(["only"])
    assert drop_item(pending, 5) is None      # queue drained between render+click
    assert drop_item(pending, -1) is None
    assert list(pending) == ["only"]          # nothing eaten


def test_drop_unlike_pull_works_even_with_a_draft_in_the_buffer():
    """pull_item deliberately refuses to clobber a draft; drop has no reason
    to care -- removing a queued line is not an edit."""
    pending, buf = deque(["a", "b"]), _Buf("draft")
    assert pull_item(pending, buf, 0) is None      # refuses, by design
    assert drop_item(pending, 0) == "a"            # still removable
    assert list(pending) == ["b"]


# ── the panel renders a ✕ per row ─────────────────────────────────────────
def test_rows_get_a_clickable_cross_when_drop_is_wired():
    pending = deque(["alpha", "beta"])
    frags = queue_fragments(pending, pull=lambda i: None, width=60,
                            drop=lambda i: None)
    crosses = [f for f in frags if f[1] == " ✕" and f[0] == "class:qp.drop"]
    assert len(crosses) == 2                   # one per row
    assert all(len(f) == 3 and callable(f[2]) for f in crosses)


def test_no_cross_and_no_hint_when_drop_is_not_wired():
    frags = queue_fragments(deque(["alpha"]), pull=lambda i: None, width=60)
    assert not [f for f in frags if f[0] == "class:qp.drop"]
    assert "✕ remove" not in "".join(t for _, t, *_ in frags)


def test_each_cross_removes_ITS_OWN_row():
    pending = deque(["alpha", "beta", "gamma"])
    frags = queue_fragments(pending, pull=lambda i: None, width=60,
                            drop=lambda i: drop_item(pending, i))
    crosses = [f for f in frags if f[0] == "class:qp.drop"]
    crosses[1][2](_MouseUp())                  # click the MIDDLE row's ✕
    assert list(pending) == ["alpha", "gamma"]


class _MouseUp:
    from prompt_toolkit.mouse_events import MouseEventType
    event_type = MouseEventType.MOUSE_UP


# ── /queue drop|edit N: the keyboard twin ─────────────────────────────────
def test_queue_drop_routes_with_a_zero_based_index():
    res = dispatch("/queue drop 2", _ctx(["a", "b", "c"]))
    assert (res.action, res.arg) == ("queue_drop", "2")


def test_queue_edit_routes():
    assert dispatch("/queue edit 1", _ctx(["a", "b"])).action == "queue_edit"


def test_queue_drop_aliases():
    for verb in ("drop", "remove", "rm"):
        assert dispatch(f"/queue {verb} 1", _ctx(["a"])).action == "queue_drop"


def test_queue_drop_rejects_an_out_of_range_number_honestly():
    res = dispatch("/queue drop 9", _ctx(["a", "b"]))
    assert res.action == "queue"                # no mutation is routed
    assert "No queued item #9" in res.message
    assert "holds 2" in res.message


def test_queue_drop_rejects_junk_and_a_missing_number():
    assert "Usage:" in dispatch("/queue drop", _ctx(["a"])).message
    assert dispatch("/queue drop abc", _ctx(["a"])).action == "queue"


def test_plain_queue_listing_still_works_and_advertises_the_new_verbs():
    res = dispatch("/queue", _ctx(["a", "b"]))
    assert res.action == "queue"
    assert "1. a" in res.message and "2. b" in res.message
    assert "/queue drop" in res.message


def test_queue_clear_is_untouched():
    res = dispatch("/queue clear", _ctx(["a", "b"]))
    assert res.action == "queue_clear" and "2 dropped" in res.message


def test_one_line_reserves_room_so_the_cross_is_never_pushed_offscreen():
    """The row text must truncate BEFORE the ✕ column, not shove it away."""
    frags = queue_fragments(deque(["x" * 200]), pull=lambda i: None, width=40,
                            drop=lambda i: None)
    row = next(t for st, t, *_ in frags if st in ("class:qp.last", "class:qp.item")
               and t.startswith("\n"))
    assert len(row.lstrip("\n")) <= 40 - 2      # ✕ column survived
