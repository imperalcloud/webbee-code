from webbee.sizing import get_size, input_height_cap, panel_cap, trunc


def test_get_size_prefers_pt_output():
    class _Out:
        def get_size(self):
            class S: columns, rows = 133, 41
            return S()
    class _App: output = _Out()
    assert get_size(_App()) == (133, 41)


def test_get_size_falls_back_without_app(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fb: type("S", (), {"columns": 91, "lines": 22})())
    assert get_size(None) == (91, 22)


def test_input_height_cap_is_proportional():
    assert input_height_cap(24) == 7      # 24*3//10
    assert input_height_cap(60) == 10     # ceiling holds
    assert input_height_cap(3) == 1       # floor holds


def test_panel_cap_and_trunc():
    assert panel_cap(24, 5) == 5          # queue floor: today's look at 24 rows
    assert panel_cap(24, 6) == 6          # todo floor: today's look at 24 rows
    assert panel_cap(60, 5) == 10         # tall screen: grows past the floor
    assert trunc(120, 0.33, 40) == 40     # floor wins on narrow
    assert trunc(200, 0.33, 40) == 66
