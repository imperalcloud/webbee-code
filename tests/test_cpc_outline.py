"""CPC outline / read_symbol and index freshness (truth-loop stage 7, client side).

Live 2026-10-18: a 1459-line declarative screen was read in 40-120 line windows ~190
times (the index knew 3 definitions in it); the code tools went unused; `index_fresh`
was false in both repos; `.next/static/chunks` reached the index; endpoint_count was 0
in a repo with a FastAPI gateway."""
import os

import pytest

pytest.importorskip("tree_sitter")

from webbee.intel import outline as ol            # noqa: E402
from imperal_sdk.code_index import extract_contracts  # noqa: E402
from webbee.intel.service import IntelService, _walk, ignored_path  # noqa: E402
from webbee.intel.watch import _ignored          # noqa: E402
from webbee.tools import LocalToolExecutor        # noqa: E402


def _screen(pad: int = 0) -> str:
    def part(name, ctor, n):
        rows = "\n".join(f"            Text('row {i}')," for i in range(n))
        return f"    {name} = {ctor}(\n        gap=4,\n        children=[\n{rows}\n        ],\n    )\n"
    filler = "".join(f"    note_{i} = {i}\n" for i in range(pad))
    return ("from ui import Stack, Modal, Popover, Text\n\n\n"
            "def account_screen(user):\n"
            + part("security_content", "Stack", 300)
            + part("active_sessions_popover", "Popover", 200)
            + part("profile_form", "Modal", 100) + filler
            + "    return Stack(children=[security_content, active_sessions_popover])\n")


def test_outline_names_the_parts_of_a_declarative_screen():
    names = {n["name"] for n in ol.outline("screens.py", _screen())}
    assert {"account_screen", "account_screen.security_content", "account_screen.active_sessions_popover"} <= names


def test_a_big_code_file_read_without_a_window_answers_with_its_outline(tmp_path):
    (tmp_path / "screens.py").write_text(_screen(pad=4600))                # over the 5000-line whole read
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_file", {"path": "screens.py"})
    assert r["ok"] and r["outline"] is True
    assert r["content"].startswith("OUTLINE of screens.py") and "⟦" not in r["content"]
    assert "account_screen.security_content = Stack(" in r["content"]
    win = ex.run("read_file", {"path": "screens.py", "offset": 10, "limit": 5})
    assert "outline" not in win and "[showing lines 10-14" in win["content"]
    (tmp_path / "small.py").write_text("x = 1\n")
    assert "outline" not in ex.run("read_file", {"path": "small.py"})


def test_read_symbol_reads_one_part_whole_in_the_read_card_form(tmp_path):
    (tmp_path / "screens.py").write_text(_screen())
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_symbol", {"path": "screens.py", "name": "security_content"})
    assert r["ok"] and r["content"].startswith("⟦ screens.py · ")
    body = r["content"].split("\n", 1)[1]
    assert body.lstrip().startswith("security_content = Stack(") and "active_sessions_popover" not in body
    assert r["shown_to"] - r["shown_from"] >= 300
    assert ex.run("read_symbol", {"path": "screens.py", "name": "nope"})["ok"] is False
    assert ex.run("outline", {"path": "screens.py"})["ok"]


def test_the_agents_own_edit_is_indexed_at_once_and_the_index_is_fresh(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    svc = IntelService(str(tmp_path), "rk", cache_dir=str(tmp_path / ".c"))
    svc.build()
    assert svc.repo_profile()["index_fresh"] is True          # a workspace without git is fresh too
    ex = LocalToolExecutor(str(tmp_path), indexer=svc)
    assert ex.run("edit_file", {"path": "a.py", "old": "def alpha", "new": "def beta"})["ok"]
    assert "beta" in svc.graph.symbol_table and "alpha" not in svc.graph.symbol_table
    assert ex.run("write_file", {"path": "b.py", "content": "def gamma():\n    pass\n"})["ok"]
    assert "gamma" in svc.graph.symbol_table


def test_the_walk_and_the_watcher_skip_the_same_paths(tmp_path):
    for rel in (".next/static/chunks/x.js", "node_modules/m/i.js", "venv/lib/x.py", "src/app.py"):
        os.makedirs(tmp_path / os.path.dirname(rel), exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n")
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = /usr\n")
    walked = set(_walk(str(tmp_path)))
    assert walked == {os.path.join("src", "app.py")}
    root = str(tmp_path)
    for rel in (".next/static/chunks/x.js", "node_modules/m/i.js", "venv/lib/x.py", ".git/index"):
        assert _ignored(os.path.join(root, rel), root) and ignored_path(root, rel)
    assert not _ignored(os.path.join(root, "src", "app.py"), root)


def test_fastapi_routers_with_prefixes_and_next_routes_are_endpoints():
    py = ('from fastapi import APIRouter\nrouter = APIRouter(prefix="/api/sessions")\n'
          '@router.get("")\nasync def list_sessions(): ...\n'
          '@router.delete("/{sid}")\nasync def kill(sid: str): ...\n'
          '@router.api_route("/x", methods=["GET", "POST"])\ndef x(): ...\n'
          'def h(): ...\nrouter.add_api_route("/h", h, methods=["PUT"])\n')
    got = {(e.method, e.route) for e in extract_contracts("gw/sessions.py", py, "python")[0]}
    assert got == {("GET", "/api/sessions"), ("DELETE", "/api/sessions/{sid}"), ("GET", "/api/sessions/x"),
                   ("POST", "/api/sessions/x"), ("PUT", "/api/sessions/h")}
    ts = "export async function GET(req) {}\nexport const DELETE = async () => {}\n"
    got = {(e.method, e.route) for e in
           extract_contracts("web/app/(dash)/api/sessions/[id]/route.ts", ts, "typescript")[0]}
    assert got == {("GET", "/api/sessions/[id]"), ("DELETE", "/api/sessions/[id]")}
    assert [(e.method, e.route) for e in
            extract_contracts("pages/api/hello.ts", "export default function h(){}", "typescript")[0]] == [
        ("ANY", "/api/hello")]
