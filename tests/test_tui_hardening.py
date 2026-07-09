"""TUI robustness: keep dependency download bars quiet at the entry point.

The inline renderer (PromptSession under patch_stdout) tolerates stray stderr —
it just lands in the terminal's native scrollback and can't corrupt an
alternate-screen diff (there is none anymore). We still silence the model /
huggingface download progress bars so they don't spam the transcript."""
import os

from webbee.cli import _quiet_downloads


def test_quiet_downloads_sets_env(monkeypatch):
    for k in ("HF_HUB_DISABLE_PROGRESS_BARS", "HF_HUB_DISABLE_TELEMETRY", "TOKENIZERS_PARALLELISM"):
        monkeypatch.delenv(k, raising=False)
    _quiet_downloads()
    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["TOKENIZERS_PARALLELISM"] == "false"


def test_quiet_downloads_respects_user_override(monkeypatch):
    # setdefault must NOT clobber an explicit user choice.
    monkeypatch.setenv("HF_HUB_DISABLE_PROGRESS_BARS", "0")
    _quiet_downloads()
    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "0"
