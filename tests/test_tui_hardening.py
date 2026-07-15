"""TUI robustness: no stray write can corrupt the full-screen dock.

Root cause of the observed 'overlapping/duplicated input field': the dock is a
prompt_toolkit full-screen Application that OWNS the terminal (diffs the screen),
so any external stderr write — a model-download tqdm bar, a library warning, a
background-task traceback — desyncs the diff. Two guards: (1) disable the
download progress bars at the entry point; (2) route stderr to a log file for
the dock's whole lifetime."""
import os

from webbee.boot import _open_dock_stderr_log
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


def test_dock_stderr_log_is_writable():
    f = _open_dock_stderr_log()
    try:
        f.write("boot noise\n")
        f.flush()
    finally:
        f.close()


def test_dock_stderr_log_never_raises_on_bad_cache_dir(monkeypatch):
    def _boom(*a, **k):
        raise OSError("cache dir unwritable")
    monkeypatch.setattr(os, "makedirs", _boom)
    f = _open_dock_stderr_log()   # must fall back (devnull/StringIO), never raise
    f.write("x")
    f.close()
