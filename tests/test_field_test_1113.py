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
