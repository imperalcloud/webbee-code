from webbee.newtab_mode import load_newtab_mode, save_newtab_mode


def _isolate(tmp_path, monkeypatch):
    import webbee.newtab_mode as NM
    monkeypatch.setattr(NM, "_CACHE_DIR", str(tmp_path / "webbee-cache"))


def test_none_when_no_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert load_newtab_mode() is None


def test_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    save_newtab_mode("plan")
    assert load_newtab_mode() == "plan"


def test_autopilot_downgraded_to_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    save_newtab_mode("autopilot")
    assert load_newtab_mode() == "default"


def test_save_never_raises_on_bad_dir(monkeypatch):
    import webbee.newtab_mode as NM
    monkeypatch.setattr(NM, "_CACHE_DIR", "/dev/null/nope")
    save_newtab_mode("plan")   # must not raise
    assert load_newtab_mode() is None
