import re
import webbee


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", webbee.__version__)


def test_version_matches_pyproject():
    import tomllib
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert data["project"]["version"] == webbee.__version__
