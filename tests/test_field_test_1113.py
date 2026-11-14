"""imperal-mcp field test 2026-11-13: empty doc in search answers, Russian queries ranked by noise."""
from types import SimpleNamespace

from imperal_sdk.code_index import Symbol

from webbee.intel import query as Q


def _svc(symbols, vectors=None, backend=None):
    table = {}
    for s in symbols:
        table.setdefault(s.name, []).append(s)
    return SimpleNamespace(graph=SimpleNamespace(symbol_table=table, index=None), vectors=vectors,
                           _get_backend=lambda: backend)


LOGIN = Symbol("complete_sign_in", "function", "auth.py", 1, 3, "def complete_sign_in():",
               doc="Завершить вход пользователя после подтверждения кода.")
BILL = Symbol("charge", "function", "bill.py", 1, 3, "def charge():", doc="Списать деньги со счёта.")


def test_a_search_answer_carries_the_symbols_doc():
    out = Q.search_code(_svc([LOGIN]), "complete_sign_in")
    assert out["data"]["items"][0]["doc"].startswith("Завершить вход")


def test_a_russian_query_finds_the_function_its_doc_describes():
    out = Q.search_code(_svc([LOGIN, BILL]), "завершить вход")
    assert [i["title"] for i in out["data"]["items"]] == ["complete_sign_in"]


def test_a_query_the_embedding_cannot_read_gets_no_vector_arm():
    asked = []

    class _Backend:
        def embed_query(self, q):
            asked.append(q)
            return [0.0]

    class _Vectors:
        def search(self, qv, top_n=50):
            return [("bill.py#1-3:function:charge", 0.88)]
    svc = _svc([LOGIN, BILL], vectors=_Vectors(), backend=_Backend())
    out = Q.search_code(svc, "завершить вход")
    assert asked == [] and [i["title"] for i in out["data"]["items"]] == ["complete_sign_in"]
    Q.search_code(svc, "finish sign in")
    assert asked == ["finish sign in"]



# ── field test report 2 ──

def test_a_small_files_window_not_applied_is_said_plainly(tmp_path):
    from webbee.tools import LocalToolExecutor
    f = tmp_path / "config.py"
    f.write_text("".join(f"x{i} = {i}\n" for i in range(128)))
    ex = LocalToolExecutor(str(tmp_path))
    r = ex.run("read_file", {"path": "config.py", "offset": 65, "limit": 25})
    assert "NOT applied" in r["content"].splitlines()[0]
    assert r["window_applied"] is False and r["asked_window"] == [65, 89]


def test_a_failed_commands_exit_code_is_in_its_text(tmp_path):
    from webbee.tools import LocalToolExecutor
    r = LocalToolExecutor(str(tmp_path)).run("bash", {"command": "pwd && git status"})
    assert r["ok"] is False and r["exit_code"] == 128 and r["content"].rstrip().endswith("[exit 128]")


def test_a_code_tools_root_is_honoured_or_refused(tmp_path):
    from webbee.tools import LocalToolExecutor
    (tmp_path / "core").mkdir()
    ex = LocalToolExecutor(str(tmp_path))
    assert ex._root_in_workspace("core") == ("core", "")
    assert ex._root_in_workspace(".") == ("", "")
    sub, why = ex._root_in_workspace("non_existent_dir")
    assert sub == "" and "does not exist" in why
    assert "outside the workspace" in ex._root_in_workspace("/")[1]
