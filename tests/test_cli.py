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
