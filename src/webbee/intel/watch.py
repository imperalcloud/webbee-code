"""Filesystem watcher that keeps IntelService's index warm across a session.
Fail-soft by design: watchfiles is an optional extra, so a base install must
never crash the repl -- it simply runs without live re-indexing."""
from __future__ import annotations
import asyncio
import os


def _ignored(path: str) -> bool:
    """PURE. True for VCS/vendor churn. Normalized to forward slashes so the
    filter works on Windows too (watchfiles yields backslash paths there --
    unfiltered .git churn re-indexed the repo on every checkpoint)."""
    p = path.replace(os.sep, "/").replace("\\", "/")
    # `.webbee/` = our own per-workspace scratch (pasted images, gitignore) —
    # never re-index/checkpoint on a paste (W3).
    return "/.git/" in p or "/node_modules/" in p or "/.webbee/" in p


async def watch_workspace(root: str, on_change) -> None:
    """Call on_change(set_of_relpaths) as files change. Fail-soft: if
    watchfiles is unavailable, return immediately (no watcher)."""
    try:
        from watchfiles import awatch
    except ImportError:
        return
    async for changes in awatch(root):
        rels = set()
        for _chg, path in changes:
            if _ignored(path):
                continue
            try:
                rels.add(os.path.relpath(path, root))
            except ValueError:
                pass
        if rels:
            try:
                # Off the event loop: on_change (apply_changes) does sync
                # file I/O + parse + a full graph rebuild -- run inline on a
                # big repo, every save would freeze the whole dock.
                await asyncio.to_thread(on_change, rels)
            except Exception:
                pass
