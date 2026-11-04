"""Owner 2026-11-04: the vault holds the code index, never the code -- a server's lines and exact texts are read on
the server itself (the user's own ssh), never a secret, never outside the repo."""
import asyncio

from webbee import tools as wt


def _server(monkeypatch, files: dict, grep_out: str = "", git: bool = True):
    calls = []

    async def run(host, argv, timeout=0, cap=10 ** 9):
        calls.append(argv)
        if argv[0] == "readlink":
            path = argv[-1]
            return (0, (files.get(path, {}).get("link") or path).encode() + b"\n", "") if path in files else \
                (1, b"", "No such file")
        if argv[0] == "head":
            data = files[argv[-1]]["text"].encode()
            return 0, data[:cap], ""
        if argv[0] == "git":
            return (0 if grep_out else 1, grep_out.encode(), "") if git else (128, b"", "fatal: not a git repository")
        if argv[0] == "grep":
            return 0, grep_out.replace("core/", "/srv/app/core/").encode(), ""
        return 1, b"", "?"
    monkeypatch.setattr(wt, "remote_run", run)
    return calls


def test_remote_read_takes_the_lines_and_refuses_secrets_and_escapes(monkeypatch):
    _server(monkeypatch, {"/srv/app/a.py": {"text": "x = 1\ny = 2\n"},
                          "/srv/app/notes.py": {"text": "k\n", "link": "/home/u/.ssh/id_rsa"}})
    got = asyncio.run(wt.remote_read("h", "/srv/app", "a.py"))
    assert got["text"] == "x = 1\ny = 2\n" and got["why"] == ""
    assert "never read" in asyncio.run(wt.remote_read("h", "/srv/app", ".env"))["why"]
    assert "never read" in asyncio.run(wt.remote_read("h", "/srv/app", "notes.py"))["why"]     # a link into ~/.ssh
    assert "outside" in asyncio.run(wt.remote_read("h", "/srv/app", "../../etc/passwd"))["why"]


def test_remote_grep_keeps_code_files_only_and_never_a_secret(monkeypatch):
    out = "core/a.py:3:    raise UNEXPECTED\n.env:1:UNEXPECTED=1\nREADME.md:2:UNEXPECTED\n"
    calls = _server(monkeypatch, {}, out)
    got = asyncio.run(wt.remote_grep("h", "/srv/app", "UNEXPECTED"))
    assert got["matches"] == [{"path": "core/a.py", "line": 3, "text": "    raise UNEXPECTED"}] and got["total"] == 1
    assert calls[0][:3] == ["git", "-C", "/srv/app"] and "-F" in calls[0]
    _server(monkeypatch, {}, out, git=False)
    got = asyncio.run(wt.remote_grep("h", "/srv/app", "UNEXPECTED", regex=True))
    assert [m["path"] for m in got["matches"]] == ["core/a.py"]
