import webbee.urlopen as urlopen


def test_open_url_returns_url_and_calls_webbrowser(monkeypatch):
    calls = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u, new=2: calls.append(u))
    out = urlopen.open_url("https://panel.imperal.io/billing")
    assert out == "https://panel.imperal.io/billing"
    assert calls == ["https://panel.imperal.io/billing"]


def test_open_url_never_raises(monkeypatch):
    import webbrowser
    def boom(*a, **k):
        raise RuntimeError("no display")
    monkeypatch.setattr(webbrowser, "open", boom)
    assert urlopen.open_url("https://x") == "https://x"
