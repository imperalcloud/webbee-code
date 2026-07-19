from webbee.reflow import anchor_offset, record_at_line


def test_record_at_line_finds_owning_record():
    # records of 3, 2, 5 lines -> absolute lines [0-2]=rec0 [3-4]=rec1 [5-9]=rec2
    spans = [3, 2, 5]
    assert record_at_line(spans, 0) == 0
    assert record_at_line(spans, 2) == 0
    assert record_at_line(spans, 3) == 1
    assert record_at_line(spans, 4) == 1
    assert record_at_line(spans, 5) == 2
    assert record_at_line(spans, 9) == 2


def test_record_at_line_past_end_falls_back_to_last_record():
    assert record_at_line([3, 2, 5], 999) == 2


def test_record_at_line_empty_ring_is_zero():
    assert record_at_line([], 0) == 0
    assert record_at_line([], 5) == 0


def test_anchor_offset_follow_snaps_to_max_off():
    assert anchor_offset([3, 2, 5], top_record=1, max_off=42, follow=True) == 42


def test_anchor_offset_not_following_sums_spans_before_the_record():
    # top_record=2 -> new start line = spans[0] + spans[1] = 5
    assert anchor_offset([3, 2, 5], top_record=2, max_off=999, follow=False) == 5


def test_anchor_offset_clamps_to_max_off():
    # top_record's new start line (3) would exceed a shrunk max_off (1)
    assert anchor_offset([3, 2, 5], top_record=1, max_off=1, follow=False) == 1
