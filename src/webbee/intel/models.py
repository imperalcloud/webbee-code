from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str            # "function" | "class" | "method" | "symbol"
    path: str            # workspace-relative
    start_line: int
    end_line: int
    signature: str = ""


@dataclass
class FileIndex:
    path: str
    lang: str            # "python" | "typescript" | "javascript" | "other"
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)   # imported module/path strings
    refs: list[str] = field(default_factory=list)      # referenced identifier names (for the graph)


@dataclass
class ProjectIndex:
    files: dict[str, FileIndex] = field(default_factory=dict)
    git_ref: str = ""
