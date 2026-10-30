"""Filesystem watcher that keeps IntelService's index warm across a session.
Fail-soft by design: watchfiles is an optional extra, so a base install must
never crash the repl -- it simply runs without live re-indexing."""
from __future__ import annotations
import asyncio
import logging
import os

log = logging.getLogger("webbee.intel")


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


async def watch_workspace(root: str, on_change, awatch=None, on_end=None) -> None:
    """Call on_change(set_of_relpaths) as files change. Fail-soft: if
    watchfiles is unavailable, return immediately (no watcher); if the OS cannot
    watch the tree, poll it; any other watch failure ends the watcher alone.
    Its end is reported -- on_end(reason), by default the ``watcher_ended`` of
    the object whose method on_change is (IntelService.apply_changes): before,
    a missing or crashed watcher left the index called fresh while no edit
    reached it any more."""
    if on_end is None:
        on_end = getattr(getattr(on_change, "__self__", None), "watcher_ended", None)
    reason = "the file watcher was stopped"
    try:
        reason = await _watch(root, on_change, awatch)
    finally:
        if callable(on_end):
            try:
                on_end(reason)
            except Exception:
                log.debug("watcher end report failed", exc_info=True)


async def _watch(root: str, on_change, awatch) -> str:
    """Follow the tree until the watcher ends; why it ended."""
    if awatch is None:
        try:
            from watchfiles import awatch
        except ImportError:
            return "watchfiles is not installed"
    try:
        await _follow(awatch(root), root, on_change)
        return "the file watcher ended"
    except OSError as exc:
        if "watch limit" not in str(exc) and "MaxFilesWatch" not in str(exc):
            return f"the file watcher failed: {exc}"[:300]
        try:
            await _follow(awatch(root, force_polling=True, poll_delay_ms=POLL_MS), root, on_change)
            return "polling the tree ended"
        except Exception as exc2:
            return f"polling the tree failed: {exc2}"[:300]
    except Exception as exc:
        return f"the file watcher failed: {type(exc).__name__}: {exc}"[:300]


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
                # one failed batch never ends the watcher; it is said in the log, not swallowed
                log.warning("index update from the watcher failed", exc_info=True)
