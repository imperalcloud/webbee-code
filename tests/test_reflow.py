from webbee.reflow import anchor_offset, record_at_line, records_to_drop


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


# ── W2 final-review Fix 2: anchor_offset takes `base` — the buffer lines
# preceding the ring's first record (deque eviction), which a reflow replay
# never re-wraps and never re-numbers. ─────────────────────────────────────

def test_anchor_offset_follow_snaps_to_max_off_even_with_base():
    assert anchor_offset([3, 2, 5], top_record=1, max_off=42, follow=True, base=100) == 42


def test_anchor_offset_not_following_adds_base_to_the_span_sum():
    # top_record=2 -> spans[0]+spans[1]=5, plus base=100 -> 105
    assert anchor_offset([3, 2, 5], top_record=2, max_off=999, follow=False, base=100) == 105


def test_anchor_offset_with_base_still_clamps_to_max_off():
    assert anchor_offset([3, 2, 5], top_record=1, max_off=50, follow=False, base=100) == 50


def test_anchor_offset_base_defaults_to_zero_unchanged():
    assert anchor_offset([3, 2, 5], top_record=2, max_off=999, follow=False) == 5


# ── W2 final-review Fix 1: records_to_drop — now returns (n_records,
# lines_covered) so the caller can move the actual cut UP to the nearest
# record boundary instead of splitting a record. ───────────────────────────

def test_records_to_drop_exact_boundary():
    # 3+2 == 5 lines exactly -> both records fit whole, nothing partial.
    assert records_to_drop([3, 2, 5], 5) == (2, 5)


def test_records_to_drop_mid_record_cut_moves_up():
    # 6 falls mid-third-record (3+2=5 < 6 < 5+5=10) -> the cut moves UP to
    # the record-1 boundary (covers 5 lines, not 6) rather than splitting it.
    assert records_to_drop([3, 2, 5], 6) == (2, 5)
    # 4 falls mid-second-record (3 < 4 < 3+2=5) -> only record 0 fits whole.
    assert records_to_drop([3, 2, 5], 4) == (1, 3)


def test_records_to_drop_zero_dropped_is_a_noop():
    assert records_to_drop([3, 2, 5], 0) == (0, 0)


def test_records_to_drop_empty_ring_covers_nothing():
    assert records_to_drop([], 999) == (0, 0)


def test_records_to_drop_covers_the_whole_ring():
    assert records_to_drop([3, 2, 5], 999) == (3, 10)
