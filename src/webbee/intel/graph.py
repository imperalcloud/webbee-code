from __future__ import annotations
from collections import defaultdict

from webbee.intel.models import ProjectIndex, Symbol


class CodeGraph:
    """Heuristic def/ref graph over a ProjectIndex. Name-match (no type
    resolution) — over-approximates on common names; honest per U1 spec."""

    def __init__(self, index: ProjectIndex) -> None:
        self.index = index
        self.symbol_table: dict[str, list[Symbol]] = defaultdict(list)
        self._file_refs: dict[str, set[str]] = {}
        self._defs_by_file: dict[str, set[str]] = {}
        for path, fi in index.files.items():
            self._file_refs[path] = set(fi.refs)
            self._defs_by_file[path] = {s.name for s in fi.symbols}
            for s in fi.symbols:
                self.symbol_table[s.name].append(s)

    def callers_of(self, name: str) -> list[Symbol]:
        out: list[Symbol] = []
        for path, refs in self._file_refs.items():
            if name in refs:
                out.extend(self.index.files[path].symbols)
        return out

    def callees_of(self, sym: Symbol) -> list[str]:
        refs = self._file_refs.get(sym.path, set())
        return sorted(r for r in refs if r in self.symbol_table)

    def dependents_of(self, names, depth: int = 2) -> set[str]:
        """Reverse transitive closure: files that (transitively) reference any
        of `names`. BFS over file->referenced-name edges."""
        frontier = set(names)
        seen_files: set[str] = set()
        for _ in range(max(1, depth)):
            hits = {p for p, refs in self._file_refs.items() if refs & frontier}
            new = hits - seen_files
            if not new:
                break
            seen_files |= new
            # expand: the symbols defined in the newly-hit files become the next frontier
            frontier = set()
            for p in new:
                frontier |= self._defs_by_file.get(p, set())
        return seen_files
