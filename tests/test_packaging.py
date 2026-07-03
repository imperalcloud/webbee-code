import tomllib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _proj():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_pypi_metadata_present():
    p = _proj()
    assert p["description"]
    assert p["readme"] == "README.md"
    assert p["license"]
    assert any("Programming Language :: Python :: 3" in c for c in p["classifiers"])
    assert p["urls"]["Homepage"]


def test_readme_exists_and_mentions_install():
    txt = (ROOT / "README.md").read_text()
    assert "pipx install webbee" in txt


def test_install_script_is_posix_and_uses_uv():
    txt = (ROOT / "install.sh").read_text()
    assert txt.startswith("#!/bin/sh")
    assert "uv tool install webbee" in txt
