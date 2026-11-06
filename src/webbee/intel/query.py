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
    repos = p.get("repos") or []
    title = (f"folder ({p['file_count']} files, {len(repos)} repositories: {', '.join(repos[:12])}"
             + (" ..." if len(repos) > 12 else "") + ")") if repos else f"repo ({p['file_count']} files)"
    item = {"id": p["repo_key"], "title": title, "kind": "folder" if repos else "repo", **p}
    return _env([item])


def graph_slice(svc, symbols, depth: int = 1) -> dict:
    """Each definition a symbol names, with its RESOLVED callers (file, line, the calling definition) and callees
    (what its body's references resolve to) -- the vault's rules (``imperal_sdk.code_links``). Before, the callers
    were every definition of every file where the name appeared (report 06.11 #2)."""
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    if isinstance(symbols, str):
        symbols = [symbols]
    lk = linker(svc)
    links = lk.links()
    items = []
    for name in (symbols or []):
        for path, qual in lk.symbol_targets(str(name)):
            if not qual:
                continue
            d = lk.defs(path).get(qual)
            it = {"id": f"{path}::{qual}", "title": qual, "kind": getattr(d, "kind", ""),
                  "subtitle": f"{path}:{getattr(d, 'start_line', 0)}", "signature": getattr(d, "signature", ""),
                  "path": path, "start_line": getattr(d, "start_line", 0), "end_line": getattr(d, "end_line", 0)}
            callers = [{"path": src, "user": user or "(module)", "line": line, "kind": kind}
                       for src, user, kind, line, tp, tq, _o in links
                       if tp == path and (tq == qual or tq.startswith(qual + ".")) and not (src == path and user == qual)]
            it["callers"] = callers[:20]
            it["callers_total"] = len(callers)
            it["callees"] = sorted({f"{tp}::{tq}" if tq else tp for src, user, _k, _l, tp, tq, _o in links
                                    if src == path and (user == qual or user.startswith(qual + "."))})[:20]
            items.append(it)
    if not items:
        return {**_env([]), "note": f"{', '.join(map(str, symbols or []))}: not a definition in this index"}
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
    """Map an embed-chunk id back to a search item.

    Id shape is '<path>#<start>-<end>:<kind>:<symbol>' (chunker._mk). The
    ':<kind>:<symbol>' tail was added 2026-07-25 to make ids unique -- see
    chunker._mk -- so the line range is only the part BEFORE the first ':'.
    Ids written by an older cache have no tail at all, hence the partition
    (not a required split): both shapes parse, and a schema bump means the
    old ones are normally gone anyway.

    Title/kind = the enclosing symbol's when the graph has one covering the
    chunk's line range, else the bare '<path>:<start>' / 'chunk' fallback --
    a hardcoded kind="chunk" would make every vector hit invisible to a
    kind= filter (F4), since none of them are ever literally "chunk"."""
    if "#" not in cid or "-" not in cid.rpartition("#")[2]:
        return None
    path, _, rng = cid.rpartition("#")
    rng = rng.partition(":")[0]          # drop the ':<kind>:<symbol>' tail
    start_s, end_s = rng.split("-", 1)
    try:
        start, end = int(start_s), int(end_s)
    except ValueError:
        return None
    title, kind = f"{path}:{start}", "chunk"
    idx = getattr(svc.graph, "index", None) if svc.graph is not None else None
    fi = idx.files.get(path) if idx is not None else None
    if fi is not None:
        for s in fi.symbols:
            if s.start_line <= start and s.end_line >= end:
                title, kind = s.name, s.kind
                break
    return {"id": cid, "title": title, "kind": kind,
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


def contract_slice(svc, query: str = "", kind: str = "all", k: int = 50) -> dict:
    """Return bounded, source-proven endpoint/schema evidence for this repo.

    Matching is deterministic substring search over route/method/handler/schema
    names and fields. Semantic expansion belongs to `search_code`; this tool is
    the exact contract projection used to ground cross-repo reasoning.
    """
    if svc is None or svc.index is None:
        return {"ok": False, "content": "index not ready"}
    q = (query or "").strip().lower()
    wanted = (kind or "all").strip().lower()
    limit = max(1, min(int(k or 50), 200))
    items = []
    for fi in svc.index.files.values():
        if wanted in {"all", "endpoint"}:
            for endpoint in fi.endpoints:
                hay = " ".join((str(endpoint.get("method", "")),
                                  str(endpoint.get("route", "")),
                                  str(endpoint.get("handler", "")),
                                  " ".join(endpoint.get("schema_refs") or ())))
                if q and q not in hay.lower():
                    continue
                route = str(endpoint.get("route") or "")
                method = str(endpoint.get("method") or "")
                items.append({
                    "id": f"{svc.repo_key}:endpoint:{method}:{route}:{endpoint.get('path')}:{endpoint.get('line')}",
                    "title": f"{method} {route}", "kind": "endpoint",
                    "repo_key": svc.repo_key, **endpoint,
                })
        if wanted in {"all", "schema"}:
            for schema in fi.schemas:
                hay = " ".join((str(schema.get("name", "")),
                                  str(schema.get("schema_kind", "")),
                                  str(schema.get("storage_name", "")),
                                  " ".join(schema.get("fields") or ())))
                if q and q not in hay.lower():
                    continue
                name = str(schema.get("name") or "")
                items.append({
                    "id": f"{svc.repo_key}:schema:{name}:{schema.get('path')}:{schema.get('line')}",
                    "title": name, "kind": "schema",
                    "repo_key": svc.repo_key, **schema,
                })
    items.sort(key=lambda x: (x["kind"], x["title"], x.get("path", ""), x.get("line", 0)))
    return _env(items[:limit], has_more=len(items) > limit)


# The local answer of ``impact_of_change`` computes links the way the vault's graph does (imperal_sdk.code_links,
# 2026-11-06): before, a file "depended" on a symbol when the bare NAME appeared in it, and each depth step pulled in
# every definition of every hit file -- a function nobody calls answered 481 files (06.11 report).
# which files carry scope facts and which are resolution configs: the SDK's one answer (Python, TS/JS, Vue/Svelte/Astro,
# Go, Rust, Java, Kotlin, C#, PHP, Ruby; go.mod, Cargo.toml, tsconfig, package.json ...) -- never a list of our own
_FACTS: dict = {}            # (root, path, content_hash) -> BlobFacts | None: a file parsed once per content
_FACTS_MAX = 60000
_READ_MAX = 2_000_000


def _read(root: str, rel: str):
    import os
    try:
        full = os.path.join(root, rel)
        if os.path.getsize(full) > _READ_MAX:
            return None
        with open(full, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _is_config(rel: str) -> bool:
    from imperal_sdk.code_index import is_resolution_config
    return is_resolution_config(rel)


def linker(svc):
    """The repo's resolver (``imperal_sdk.code_links.Linker``) for the index's current graph -- built once per graph
    (a rebuilt graph builds it again), each file parsed once per content."""
    import os
    g = svc.graph
    if getattr(svc, "_linker_graph", None) is g and getattr(svc, "_linker", None) is not None:
        return svc._linker
    from imperal_sdk import code_index as ci
    from imperal_sdk.code_links import Linker
    root = str(getattr(svc, "root", "") or "")
    if len(_FACTS) > _FACTS_MAX:
        _FACTS.clear()
    facts: dict = {}
    for path, fi in list(svc.index.files.items()):
        if not ci.has_scope_facts(path):
            continue
        key = (root, path, getattr(fi, "content_hash", ""))
        if key not in _FACTS:
            text = _read(root, path) if root else None
            _FACTS[key] = ci.scope_facts(path, text) if text is not None else None
        facts[path] = _FACTS[key]
    configs = []
    cands = {p for p in svc.index.files if _is_config(p)}
    if root:
        cands |= {n for n in os.listdir(root) if _is_config(n)} if os.path.isdir(root) else set()
    for rel in sorted(cands):
        text = _read(root, rel) if root else None
        if text is None:
            continue
        try:
            kind, data = ci.parse_config(rel, text)
        except Exception:
            continue
        configs.append({"path": rel.replace("\\", "/"), "kind": kind, "data": data})
    roots = []
    if root:
        try:
            from webbee.intel.service import is_repository, sub_repositories
            roots = [] if is_repository(root) else sub_repositories(root)
        except Exception:
            roots = []
    lk = Linker(facts, configs, roots)
    svc._linker, svc._linker_graph = lk, g
    return lk


def impact_of_change(svc, symbols) -> dict:
    """Files that use ``symbols`` (resolved links: line, user, depth through the enclosing definition), code before
    tests; ``by_name`` -- files that only share a name (a method on a local, a language without scope facts), marked
    and bounded; ``resolved`` -- the definitions found (none: "not a definition in this index", never "0 users")."""
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    if isinstance(symbols, str):
        symbols = [symbols]
    from imperal_sdk.code_index import has_scope_facts
    from imperal_sdk.code_links import BY_NAME_SHOWN, impact, is_test_path
    syms = [str(s) for s in (symbols or []) if str(s).strip()]
    out = impact(linker(svc), syms, depth=2)
    # languages without scope facts: their uses are known by name only -- listed with the other by-name files
    names = {s.split("::")[-1].split(":")[-1].rsplit(".", 1)[-1] for s in syms}
    known = {b["path"] for b in out["by_name"]} | {i["id"] for i in out["data"]["items"]}
    extra = []
    for path, fi in svc.index.files.items():
        if has_scope_facts(path) or path in known:
            continue
        if names & set(getattr(fi, "refs", ()) or ()):
            extra.append({"path": path, "line": 0, "name": "", "group": "tests" if is_test_path(path) else "code",
                          "note": "no scope facts for this language: by name"})
    if extra:
        out["by_name_count"] = int(out.get("by_name_count") or 0) + len(extra)
        out["by_name"] = sorted(out["by_name"] + extra, key=lambda b: (b["group"] == "tests", b["path"]))[:BY_NAME_SHOWN]
    from imperal_sdk.code_links import impact_summary
    out["summary"] = impact_summary(syms, out["resolved"], len(out["data"]["items"]), int(out.get("by_name_count") or 0),
                                    len(out.get("same_file") or []))
    if not out["resolved"]:
        out["note"] = f"{', '.join(syms)}: not a definition in this index"
    out["link"] = "resolved locally (the vault's rules); by_name lists name-only matches apart"
    return out


def orient(svc, q) -> dict:
    if svc is None or svc.graph is None:
        return {"ok": False, "content": "index not ready"}
    prof = repo_profile(svc)["data"]["items"]
    hits = search_code(svc, q, k=8)["data"]["items"]
    return _env(prof + hits)
