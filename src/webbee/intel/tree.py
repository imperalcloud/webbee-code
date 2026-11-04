"""The checksum tree of an indexed folder: one hash per directory over what is inside it, so a change is found by
comparing hashes from the top down -- a directory whose hash is equal is skipped whole, never listed file by file
(the owner, 2026-11-04: "not scan everything -- know at once what actually changed").

A file's leaf is ``name, mtime_ns, size`` (a stat, no read); a directory's hash covers its files' leaves and its
subdirectories' hashes, sorted by name. The root's hash (``tree[""]``) is the folder's checksum: equal roots, equal
listings. The same tree is what the vault keeps as the folder's index (universal index phase 3), so a sync sends the
directories whose hashes differ, never the whole listing.

A git repository has a cheaper checksum still: ``repo_checksum`` -- HEAD, ``git status`` and the stats of the files it
names. Equal checksums: nothing tracked or untracked-and-not-ignored changed, and no walk is needed at all.
"""
from __future__ import annotations

import hashlib
import os
import subprocess


def _h(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        h.update(p.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
    return h.hexdigest()


def _parent(rel: str) -> tuple[str, str]:
    d, _, name = rel.rpartition("/")
    return d, name


def dir_hashes(stats: dict) -> dict:
    """PURE. ``{relpath: [mtime_ns, size]}`` -> ``{dir: hash}`` for every directory holding an indexed file (``""`` is
    the root; its hash is the folder's checksum)."""
    files: dict[str, list] = {}
    subdirs: dict[str, set] = {"": set()}
    seen: set = set()
    for rel, st in stats.items():
        d, name = _parent(rel)
        files.setdefault(d, []).append((name, st))
        while d and d not in seen:                  # every ancestor is a directory node of its parent
            seen.add(d)
            up, dname = _parent(d)
            subdirs.setdefault(up, set()).add(dname)
            d = up
    out: dict[str, str] = {}
    for d in sorted(seen | {""}, key=lambda x: (-(x.count("/") + 1) if x else 1, x)):   # deepest first, root last
        parts = [f"f{name}:{st[0]}:{st[1]}" for name, st in sorted(files.get(d, []), key=lambda x: x[0])]
        parts += [f"d{name}:{out[f'{d}/{name}' if d else name]}" for name in sorted(subdirs.get(d, ()))]
        out[d] = _h(*parts)
    return out


def changed_files(old_stats: dict, new_stats: dict, old_tree: dict | None = None,
                  new_tree: dict | None = None) -> tuple[list, list, int]:
    """PURE. ``(changed or new files, files gone, directories compared)`` between two listings, found top down: a
    directory whose hash is equal in both trees is skipped with everything under it."""
    old_tree = old_tree if old_tree else dir_hashes(old_stats)
    new_tree = new_tree if new_tree else dir_hashes(new_stats)
    if old_tree.get("") == new_tree.get(""):
        return [], [], 1
    old_by_dir: dict[str, list] = {}
    new_by_dir: dict[str, list] = {}
    for rel in old_stats:
        old_by_dir.setdefault(_parent(rel)[0], []).append(rel)
    for rel in new_stats:
        new_by_dir.setdefault(_parent(rel)[0], []).append(rel)
    sub: dict[str, set] = {}
    for d in set(old_tree) | set(new_tree):
        if d:
            sub.setdefault(_parent(d)[0], set()).add(d)
    changed, gone, compared = [], [], 0
    todo = [""]
    while todo:
        d = todo.pop()
        compared += 1
        if d in old_tree and old_tree.get(d) == new_tree.get(d):
            continue                                # the same subtree: nothing under it changed
        for rel in new_by_dir.get(d, []):
            if old_stats.get(rel) != new_stats[rel]:
                changed.append(rel)
        for rel in old_by_dir.get(d, []):
            if rel not in new_stats:
                gone.append(rel)
        todo.extend(sub.get(d, ()))
    return sorted(changed), sorted(gone), compared


def repo_checksum(root: str) -> str:
    """A git repository's checksum without a walk: HEAD, ``git status`` (untracked files one by one, ignored ones
    never) and the stats of every path it names -- an edit to an already-modified file changes its stat. ``""`` when
    git cannot answer (no repository, no git): the caller walks."""
    if not os.path.exists(os.path.join(root, ".git")):
        return ""
    try:
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, timeout=10)
        st = subprocess.run(["git", "-C", root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                             "--ignored=no"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    if st.returncode != 0:
        return ""
    h = hashlib.blake2b(digest_size=16)
    h.update(head.stdout.strip() if head.returncode == 0 else b"no-head")
    h.update(b"\0")
    h.update(st.stdout)
    for item in st.stdout.split(b"\0"):
        path = item[3:].decode("utf-8", "surrogateescape") if len(item) > 3 else ""
        if not path:
            continue
        try:
            s = os.stat(os.path.join(root, path))
            h.update(f"{path}\0{s.st_mtime_ns}\0{s.st_size}\n".encode("utf-8", "surrogateescape"))
        except OSError:
            h.update(f"{path}\0gone\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()
