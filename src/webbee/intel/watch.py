"""Filesystem watcher that keeps IntelService's index warm across a session.
Fail-soft by design: watchfiles is an optional extra, so a base install must
never crash the repl -- it simply runs without live re-indexing."""
from __future__ import annotations
import asyncio
import os


def _ignored(path: str, root: str = "") -> bool:
    """True for paths the index never reads -- the SAME rule as the full walk
    (service.skipped_dir: every dotted directory incl. .git, .next build output
    and our own .webbee/ scratch, dependency caches, virtualenvs). Before, the
    watcher skipped only .git/node_modules/.webbee, so a `next build` fed
    .next/static/chunks into the index as "schemas". Normalized to forward
    slashes so the filter works on Windows too."""
    from webbee.intel.service import ignored_path
    return ignored_path(root, path)


# the OS's watch limit reached (Linux: fs.inotify.max_user_watches; live 2026-10-25 on a repo with node_modules: the
# watcher task died with "OS file watch limit reached" and its exception was printed when webbee quit): the tree is
# polled instead -- slower, never a crash
POLL_MS = 3000


async def watch_workspace(root: str, on_change, awatch=None) -> None:
    """Call on_change(set_of_relpaths) as files change. Fail-soft: if
    watchfiles is unavailable, return immediately (no watcher); if the OS cannot
    watch the tree, poll it; any other watch failure ends the watcher alone."""
    if awatch is None:
        try:
            from watchfiles import awatch
        except ImportError:
            return
    try:
        await _follow(awatch(root), root, on_change)
    except OSError as exc:
        if "watch limit" not in str(exc) and "MaxFilesWatch" not in str(exc):
            return
        try:
            await _follow(awatch(root, force_polling=True, poll_delay_ms=POLL_MS), root, on_change)
        except Exception:
            return
    except Exception:
        return


async def _follow(stream, root: str, on_change) -> None:
    async for changes in stream:
        rels = set()
        for _chg, path in changes:
            if _ignored(path, root):
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
