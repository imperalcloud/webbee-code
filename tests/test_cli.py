import pytest
from webbee.cli import build_parser


def test_version_flag(capsys):
    from webbee.cli import main
    import webbee
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert webbee.__version__ in capsys.readouterr().out


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.mode == "default" and args.cmd is None


def test_parser_login_subcommand():
    args = build_parser().parse_args(["login"])
    assert args.cmd == "login"


def test_parser_mode_choice():
    args = build_parser().parse_args(["--mode", "autopilot"])
    assert args.mode == "autopilot"


def test_parser_rejects_bad_mode():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mode", "turbo"])


def test_print_boot_progress_writes_on_a_tty(monkeypatch, capsys):
    """webbee-code-boot-visibility-v1 (Valentin, live, Linux boxes: "просто
    висит в терминале webbee и хуй поймешь, работает или комп завис"): a
    one-line progress note must reach stdout while a tty is attached, so a
    slow update-check or a big-repo index no longer looks like a hang."""
    from webbee.cli import _print_boot_progress
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    _print_boot_progress("checking for updates")
    out = capsys.readouterr().out
    assert "checking for updates" in out
    assert "🐝" in out


def test_print_boot_progress_silent_on_non_tty(monkeypatch, capsys):
    """Piped/redirected output (CI, `webbee | tee log`) must NOT get an
    overwriting \\r progress note -- it would just print as garbage."""
    from webbee.cli import _print_boot_progress
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    _print_boot_progress("checking for updates")
    assert capsys.readouterr().out == ""
