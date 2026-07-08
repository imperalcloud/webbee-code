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


_RRF_K = 60
_fnmatch = fnmatch.fnmatch


def _fuse_rrf(arms: list[list[str]], k: int = _RRF_K) -> list[str]:
    """Reciprocal-rank fusion over N ranked id lists -- pure, no ties broken
    by anything but rank (an id present in multiple arms outranks a
    single-arm id, which is the whole point of hybridizing)."""
    score: dict[str, float] = {}
    for arm in arms:
        for rank, _id in enumerate(arm):
            score[_id] = score.get(_id, 0.0) + 1.0 / (k + rank + 1)
    return [i for i, _ in sorted(score.items(), key=lambda kv: -kv[1])]


def _lexical_search(svc, q, kind, path_glob, items_by_id: dict) -> list[str]:
    """U1 substring-over-symbol-table match, extracted verbatim so it can
    serve as its own RRF arm (and the whole answer when vectors are absent)."""
    ql, hits = (q or "").lower(), []
    for name, syms in svc.graph.symbol_table.items():
        if ql in name.lower():
            for s in syms:
                if kind and s.kind != kind:
                    continue
                if path_glob and not _fnmatch(s.path, path_glob):
                    continue
                hits.append(_sym_item(s))
    hits.sort(key=lambda i: (i["title"].lower() != ql, len(i["title"])))
    ids = []
    for it in hits:
        items_by_id[it["id"]] = it
        ids.append(it["id"])
    return ids


def _chunk_item(svc, cid: str, score: float) -> dict | None:
    """Map an embed-chunk id ('<path>#<start>-<end>') back to a search item.
    Title = the enclosing symbol name when the graph has one covering the
    chunk's line range, else the bare '<path>:<start>' fallback."""
    if "#" not in cid or "-" not in cid.rpartition("#")[2]:
        return None
    path, _, rng = cid.rpartition("#")
    start_s, end_s = rng.split("-", 1)
    try:
        start, end = int(start_s), int(end_s)
    except ValueError:
        return None
    title = f"{path}:{start}"
    idx = getattr(svc.graph, "index", None) if svc.graph is not None else None
    fi = idx.files.get(path) if idx is not None else None
    if fi is not None:
        for s in fi.symbols:
            if s.start_line <= start and s.end_line >= end:
                title = s.name
                break
    return {"id": cid, "title": title, "kind": "chunk",
            "subtitle": f"{path}:{start}", "signature": "",
            "path": path, "start_line": start, "end_line": end, "score": score}


def search_code(svc, q, k: int = 20, kind=None, path_glob=None) -> dict:
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    items_by_id: dict = {}
    # lexical arm (U1) -- also the whole answer when vectors are absent
    lex_ids = _lexical_search(svc, q, kind, path_glob, items_by_id)
    arms = [lex_ids]
    # vector arm (hybrid) -- skipped gracefully when unavailable
    # (I-CPC-SEARCH-HYBRID-FALLSOFT: no vectors/backend/embed error must
    # never turn into an error envelope -- fall through to lexical+graph).
    try:
        b = svc._get_backend() if getattr(svc, "vectors", None) is not None else None
        if b is not None and svc.vectors is not None:
            qv = b.embed_query(q)
            vec_hits = svc.vectors.search(qv, top_n=50)
            vec_ids = []
            for cid, sc in vec_hits:
                it = _chunk_item(svc, cid, sc)
                if it is None:
                    continue
                if kind and it["kind"] != kind:
                    continue
                if path_glob and not _fnmatch(it["path"], path_glob):
                    continue
                items_by_id[it["id"]] = it
                vec_ids.append(it["id"])
            arms.append(vec_ids)
    except Exception:
        pass    # any embed/vector error -> lexical+graph only (never break the turn)
    # graph arm: 1-hop expansion deferred to U2.6 (no-op for now, per plan)
    fused = _fuse_rrf(arms)
    hits = [items_by_id[i] for i in fused if i in items_by_id][:k]
    return _env(hits, has_more=len(fused) > k)


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
