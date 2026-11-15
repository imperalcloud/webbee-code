"""The fixture's truth is what the disk and git say, never what a product says."""
import subprocess

from ci.field_gate.fixture import build_fixture


def _git_files(repo):
    out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True)
    return sorted(out.stdout.split())


def test_fixture_truth_matches_disk(tmp_path):
    fx = build_fixture(tmp_path / "ws")
    assert not (fx.root / ".git").exists()                       # a folder of repos, not a repo (Ignat's wpanel)
    assert set(fx.repos) == {"app", "app-gateway", "lib-sdk"}
    assert fx.repos["app-gateway"].name.startswith(fx.repos["app"].name)   # one name prefixes the other
    for rel, n in fx.truth.lines.items():
        data = (fx.root / rel).read_bytes()
        assert n == data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), rel
    for name, repo in fx.repos.items():
        assert fx.truth.git_files[name] == _git_files(repo)
        assert fx.truth.heads[name] == subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def test_fixture_has_the_reported_shapes(tmp_path):
    fx = build_fixture(tmp_path / "ws")
    t = fx.truth
    assert t.lines["core/app/mid.py"] == 600
    assert t.lines["core/app-gateway/big.py"] == 6001
    assert t.lines["core/app/no_nl.py"] == 2 and not (fx.root / "core/app/no_nl.py").read_bytes().endswith(b"\n")
    assert b"\r\n" in (fx.root / "core/app/crlf.py").read_bytes()
    assert "Сверяет права доступа сессии" in (fx.root / t.ru_doc.path).read_text(encoding="utf-8")
    assert "handleSubmit" in (fx.root / t.nested_ts.path).read_text(encoding="utf-8")
    assert '@door("/v1/x")' in (fx.root / t.door_route.path).read_text(encoding="utf-8")
    assert "Emitter" in (fx.root / t.reexport.reexport_path).read_text(encoding="utf-8")


def test_fixture_is_deterministic(tmp_path):
    a = build_fixture(tmp_path / "a")
    b = build_fixture(tmp_path / "b")
    assert a.truth.heads == b.truth.heads                         # fixed author, date and content
    assert a.truth.lines == b.truth.lines


def test_secrets_live_only_where_planted(tmp_path):
    fx = build_fixture(tmp_path / "ws")
    assert fx.truth.secrets
    remote = subprocess.run(["git", "-C", str(fx.repos["app-gateway"]), "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=True).stdout
    assert any(s in remote for s in fx.truth.secrets)
    assert any(s in (fx.repos["app-gateway"] / ".env").read_text() for s in fx.truth.secrets)
    assert ".env" not in fx.truth.git_files["app-gateway"]          # present on disk, never committed
