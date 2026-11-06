import pathlib

import pytest

# tomllib is 3.11+; on 3.10 these checks run where the release is built
tomllib = pytest.importorskip("tomllib")

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
    # 0.3.51: the index + semantic arm are BASE dependencies, so the installer
    # no longer needs extras. The [intel]/[intel-embed] names survive as empty
    # aliases, so an OLD copy of this script still works -- but the shipped one
    # must be the plain form.
    # 2026-11-06: always the newest release, from git -- the three packages by their stable branch (never a version,
    # so `uv tool upgrade` and a second run bring the newest); without git the plain form from PyPI
    for spec in ('"webbee @ git+$GH/webbee-code.git@stable"', '"imperal-mcp[code] @ git+$GH/imperal-mcp.git@stable"',
                 '"imperal-sdk[universal-index] @ git+$GH/imperal-sdk.git@stable"'):
        assert spec in txt
    assert 'uv tool install --force --refresh "webbee"' in txt
    assert "webbee[intel" not in txt, "extras are redundant since 0.3.51"
