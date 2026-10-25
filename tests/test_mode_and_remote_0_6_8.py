"""0.6.8: the kernel hears the coding mode (autopilot asks nothing -- owner 2026-10-25: "remove the cards, they stall
her"); a server's repo is synced only where it is (a missing root walked /home for 30 min) and its HEAD is cheap."""
import asyncio
from types import SimpleNamespace

import webbee.tools as wt


class _Entry:
    def __init__(self, name):
        self.filename = name


class _Sftp:
    def __init__(self, tree, files=None):
        self.tree, self.files = tree, files or {}

    async def readdir(self, path):
        if path not in self.tree:
            raise FileNotFoundError(path)
        return [_Entry(n) for n in self.tree[path]]


def test_a_missing_root_is_refused_before_any_walk(monkeypatch):
    from imperal_sdk import code_index as ci
    sftp = _Sftp({"/home": ["ipw-repo"], "/": ["home"]})
    why = asyncio.run(wt._no_repo_at(sftp, ci, "/home/no-such-repo"))
    assert "does not exist" in why


def test_a_folder_in_no_repository_is_refused(monkeypatch):
    from imperal_sdk import code_index as ci
    sftp = _Sftp({"/srv/photos": ["a.jpg"], "/srv": ["photos"], "/": ["srv"]})
    assert "in no repository" in asyncio.run(wt._no_repo_at(sftp, ci, "/srv/photos"))
    repo = _Sftp({"/r": [".git", "a.py"], "/": ["r"]})
    assert asyncio.run(wt._no_repo_at(repo, ci, "/r")) == ""


def test_remote_sync_of_a_missing_root_says_why_and_lists_nothing(monkeypatch):
    sftp = _Sftp({"/": ["home"], "/home": []})

    async def opened(host):
        return sftp, None, [], ""
    monkeypatch.setattr(wt, "_open_sftp", opened)
    out = asyncio.run(wt.remote_sync(None, "h", "/home/nope", []))
    assert out["repo_key"] == "" and "does not exist" in out["partial_reason"]


def test_remote_head_reads_the_commit(monkeypatch):
    from imperal_sdk import code_index as ci
    sftp = _Sftp({"/r": [".git"], "/": ["r"]})

    async def opened(host):
        return sftp, None, [], ""

    async def meta(s, root):
        return {"files": {".git/HEAD": b"ref: refs/heads/master\n", ".git/refs/heads/master": b"fd751713" + b"0" * 32}}
    monkeypatch.setattr(wt, "_open_sftp", opened)
    monkeypatch.setattr(ci, "sftp_read_meta", meta)
    got = asyncio.run(wt.remote_head("h", "/r"))
    assert got["commit"].startswith("fd751713") and got["branch"] == "master" and got["why"] == ""

    async def unreachable(host):
        return None, None, [], "OSError: Name or service not known"
    monkeypatch.setattr(wt, "_open_sftp", unreachable)
    assert "Name or service not known" in asyncio.run(wt.remote_head("nope", "/r"))["why"]


def test_a_mode_switch_reaches_the_running_session(monkeypatch):
    import webbee.repl as R
    sent = []

    class _Agent:
        session_id = "imp_u_a-r1"
        mode = "default"

        async def post_mode(self, mode):
            sent.append(mode)
            return True
    slot = SimpleNamespace(mode="default", agent=_Agent(), workspace="/w")
    monkeypatch.setattr("webbee.mode_store.save_mode", lambda ws, m: None)

    async def go():
        R.set_slot_mode(slot, "autopilot")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(go())
    assert slot.agent.mode == "autopilot" and sent == ["autopilot"]


def test_no_session_yet_sends_nothing(monkeypatch):
    import webbee.repl as R

    class _Agent:
        session_id = ""
        mode = "default"

        async def post_mode(self, mode):
            raise AssertionError("posted without a session")
    slot = SimpleNamespace(mode="default", agent=_Agent(), workspace="/w")
    monkeypatch.setattr("webbee.mode_store.save_mode", lambda ws, m: None)

    async def go():
        R.set_slot_mode(slot, "plan")
        await asyncio.sleep(0)
    asyncio.run(go())
    assert slot.agent.mode == "plan"
