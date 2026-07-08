from __future__ import annotations
import fnmatch


def _env(items: list[dict], has_more: bool = False) -> dict:
    return {"ok": True, "data": {"items": items, "total": len(items), "has_more": has_more}}


def _sym_item(s) -> dict:
    return {"id": f"{s.path}::{s.name}", "title": s.name, "kind": s.kind,
            "subtitle": f"{s.path}:{s.start_line}", "signature": s.signature,
            "path": s.path, "start_line": s.start_line, "end_line": s.end_line}


def repo_profile(svc) -> dict:
    p = svc.repo_profile()
    item = {"id": p["repo_key"], "title": f"repo ({p['file_count']} files)", "kind": "repo", **p}
    return _env([item])


def graph_slice(svc, symbols, depth: int = 1) -> dict:
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    if isinstance(symbols, str):
        symbols = [symbols]
    g, items = svc.graph, []
    for name in (symbols or []):
        for s in g.symbol_table.get(name, []):
            it = _sym_item(s)
            it["callers"] = [c.name for c in g.callers_of(name)][:20]
            it["callees"] = g.callees_of(s)[:20]
            items.append(it)
    return _env(items)


def search_code(svc, q, k: int = 20, kind=None, path_glob=None) -> dict:
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    ql, hits = (q or "").lower(), []
    for name, syms in svc.graph.symbol_table.items():
        if ql in name.lower():
            for s in syms:
                if kind and s.kind != kind:
                    continue
                if path_glob and not fnmatch.fnmatch(s.path, path_glob):
                    continue
                hits.append(_sym_item(s))
    hits.sort(key=lambda i: (i["title"].lower() != ql, len(i["title"])))
    return _env(hits[:k], has_more=len(hits) > k)


def impact_of_change(svc, symbols) -> dict:
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    if isinstance(symbols, str):
        symbols = [symbols]
    files = svc.graph.dependents_of(list(symbols or []), depth=3)
    items = [{"id": f, "title": f, "kind": "file"} for f in sorted(files)]
    return _env(items)


def orient(svc, q) -> dict:
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    prof = repo_profile(svc)["data"]["items"]
    hits = search_code(svc, q, k=8)["data"]["items"]
    return _env(prof + hits)
