from __future__ import annotations
import hashlib
import os
from dataclasses import dataclass

from webbee.intel.models import FileIndex, ProjectIndex

_CHUNK_MAX_LINES = 60      # a symbol longer than this is split into windows
_WINDOW_LINES = 60
_WINDOW_OVERLAP = 10


@dataclass(frozen=True)
class Chunk:
    id: str
    symbol: str            # symbol name, or "" for a windowed/gap chunk
    kind: str              # "function" | "class" | "chunk"
    path: str
    start_line: int
    end_line: int
    text: str
    content_hash: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _mk(path: str, symbol: str, kind: str, s: int, e: int, lines: list[str]) -> Chunk:
    text = "\n".join(lines[s - 1:e])
    return Chunk(id=f"{path}#{s}-{e}", symbol=symbol, kind=kind, path=path,
                 start_line=s, end_line=e, text=text, content_hash=_hash(text))


def _windows(path: str, symbol: str, s: int, e: int, lines: list[str]) -> list[Chunk]:
    out, cur = [], s
    while cur <= e:
        end = min(cur + _WINDOW_LINES - 1, e)
        out.append(_mk(path, symbol, "chunk", cur, end, lines))
        if end >= e:
            break
        cur = end - _WINDOW_OVERLAP + 1
    return out


def chunk_file(root: str, fi: FileIndex) -> list[Chunk]:
    try:
        with open(os.path.join(root, fi.path), "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except (OSError, UnicodeDecodeError):
        return []
    n = len(lines)
    out: list[Chunk] = []
    covered = [False] * (n + 2)
    syms = sorted(fi.symbols, key=lambda s: s.start_line)
    for sym in syms:
        s, e = max(1, sym.start_line), min(n, sym.end_line)
        if e - s + 1 > _CHUNK_MAX_LINES:
            out.extend(_windows(fi.path, sym.name, s, e, lines))
        else:
            out.append(_mk(fi.path, sym.name, sym.kind, s, e, lines))
        for ln in range(s, e + 1):
            covered[ln] = True
    # cover no-symbol gaps (module code, config, parse-failed line-only files) with windows
    ln = 1
    while ln <= n:
        if covered[ln] or not lines[ln - 1].strip():
            ln += 1
            continue
        gs = ln
        while ln <= n and not covered[ln]:
            ln += 1
        out.extend(_windows(fi.path, "", gs, ln - 1, lines))
    return out


def chunk_index(root: str, index: ProjectIndex) -> list[Chunk]:
    out: list[Chunk] = []
    for fi in index.files.values():
        out.extend(chunk_file(root, fi))
    return out
