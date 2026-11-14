import json
from webbee.update import check_for_update


def test_notice_when_newer(tmp_path):
    msg = check_for_update("0.1.0", cache_path=tmp_path / "u.json", now=1000.0,
                           fetch=lambda: "0.2.0")
    assert msg and "0.2.0" in msg and "-m pip install -U webbee" in msg     # this test venv is a plain pip install


def test_none_when_current_is_latest(tmp_path):
    assert check_for_update("0.2.0", cache_path=tmp_path / "u.json", now=1000.0,
                            fetch=lambda: "0.2.0") is None


def test_offline_fetch_failure_is_silent(tmp_path):
    assert check_for_update("0.1.0", cache_path=tmp_path / "u.json", now=1000.0,
                            fetch=lambda: None) is None


def test_uses_cache_within_ttl_without_fetching(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text(json.dumps({"latest": "0.5.0", "checked_at": 900.0}))
    calls = []
    msg = check_for_update("0.1.0", cache_path=cache, now=1000.0,
                           fetch=lambda: calls.append(1) or "9.9.9", ttl=86400.0)
    assert calls == []          # cache fresh → no network
    assert "0.5.0" in msg


def test_refetches_after_ttl(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text(json.dumps({"latest": "0.5.0", "checked_at": 0.0}))
    msg = check_for_update("0.1.0", cache_path=cache, now=1_000_000.0,
                           fetch=lambda: "0.6.0", ttl=86400.0)
    assert "0.6.0" in msg


def test_malformed_cache_is_ignored(tmp_path):
    cache = tmp_path / "u.json"
    cache.write_text("{not json")
    msg = check_for_update("0.1.0", cache_path=cache, now=1000.0, fetch=lambda: "0.2.0")
    assert "0.2.0" in msg


def test_raising_fetch_is_swallowed(tmp_path):
    def boom():
        raise RuntimeError("network exploded")
    assert check_for_update("0.1.0", cache_path=tmp_path / "u.json", now=1000.0, fetch=boom) is None


# live 2026-11-14: "they install a version and webbee says a newer one is out, though they run the newest"

def test_the_upgrade_hint_is_the_tool_that_installed_it():
    from webbee.update import upgrade_hint
    assert upgrade_hint("/home/u/.local/share/uv/tools/webbee") == "uv tool upgrade webbee"
    assert upgrade_hint("C:\\Users\\a\\pipx\\venvs\\webbee") == "pipx upgrade webbee"
    assert upgrade_hint("C:\\Program Files\\Python311", "C:\\Program Files\\Python311\\python.exe") == \
        '"C:\\Program Files\\Python311\\python.exe" -m pip install -U webbee'


def test_a_version_with_a_suffix_is_read_never_always_older():
    from webbee.update import _ver
    assert _ver("0.7.16") == (0, 7, 16) and _ver("v0.7.16") == (0, 7, 16)
    assert _ver("0.7.16rc1") == (0, 7, 16) and _ver("0.7.16.dev0") == (0, 7, 16) and _ver("0.7.16+local") == (0, 7, 16)
    assert _ver("") == () and _ver(None) == ()
    assert check_for_update("0.7.16+win", cache_path="/nonexistent/u.json", now=1.0, fetch=lambda: "0.7.16") is None
    assert check_for_update("garbage", cache_path="/nonexistent/u.json", now=1.0, fetch=lambda: "0.7.16") is None


def test_the_newest_comes_from_the_stable_branch_like_the_upgrade(monkeypatch):
    from webbee import autoupdate as au
    from webbee import update
    monkeypatch.delenv(au.LIGHT_ENV, raising=False)
    monkeypatch.setattr(au, "safe_which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(au, "stable_version", lambda dist, git: "0.7.16")
    assert update.default_fetch() == "0.7.16"           # PyPI is not asked when git answers


def test_installed_version_reads_the_running_package_never_a_stray_dist_info(monkeypatch):
    import importlib.metadata as md
    from webbee import __version__, update
    monkeypatch.setattr(md, "version", lambda name: "0.3.49")   # an old dist-info first on sys.path
    calls = []
    assert update.installed_version(run=lambda *a, **k: calls.append(1)) == __version__ and calls == []
    assert update.restart_notice(__version__, update.installed_version()) == ""


def test_a_uv_receipt_on_the_branch_is_read_however_uv_writes_it(tmp_path):
    from webbee import autoupdate as au
    for ref in ("?rev=stable", "@stable", "#stable"):
        (tmp_path / "uv-receipt.toml").write_text("[tool]\nrequirements = [\n" + "".join(
            f'    {{ name = "{n}", git = "{u}{ref}" }},\n' for n, u in (
                ("webbee", "https://github.com/imperalcloud/webbee-code.git"),
                ("imperal-mcp", "https://github.com/imperalcloud/imperal-mcp.git"),
                ("imperal-sdk", "https://github.com/imperalcloud/imperal-sdk.git"))) + "]\n")
        assert au.on_stable("uv", str(tmp_path), au.WEBBEE_ENV_SPECS), ref
    (tmp_path / "uv-receipt.toml").write_text('requirements = [ { name = "webbee", version = "0.7.15" } ]\n')
    assert not au.on_stable("uv", str(tmp_path), au.WEBBEE_ENV_SPECS)      # recorded by a version: moved once
