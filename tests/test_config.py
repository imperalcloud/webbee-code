import os
from webbee.config import Config

def test_from_env_defaults(monkeypatch):
    monkeypatch.delenv("IMPERAL_API_URL", raising=False)
    monkeypatch.delenv("IMPERAL_PANEL_URL", raising=False)
    cfg = Config.from_env()
    assert cfg.api_url == "https://auth.imperal.io"
    assert cfg.panel_url == "https://panel.imperal.io"

def test_from_env_override(monkeypatch):
    monkeypatch.setenv("IMPERAL_API_URL", "http://localhost:8080")
    cfg = Config.from_env()
    assert cfg.api_url == "http://localhost:8080"
