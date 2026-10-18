"""A code file read by meaning, not by windows (CPC ``outline`` / ``read_symbol``).

The same tree-sitter packages as the index (``indexer.py``) give, for one file:
- ``outline``: the definitions AND the significant nodes inside them with line ranges --
  a big assignment (``security_content = Stack(``), a big keyword argument or object
  property, a big returned tree, a comment header. For a declarative UI file this is its map;
- ``read_symbol``: exactly one such node, whole (in the read_file card form);
- ``syntax_errors``: the first ERROR / MISSING nodes.

Kept in step with the kernel's ``imperal_kernel/core/code_outline.py`` (it parses remote
files the same way). Fail-soft: no parser -> None, never a crash."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

LANG_BY_EXT = {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
               ".mjs": "javascript", ".cjs": "javascript"}
BIG_LINES = 12            # an inner node this long is a region of the file worth naming
MAX_NODES = 400           # one outline never floods a frame
_HEADER = re.compile(r"^\s*(?:#|//|/\*+)\s*(?:[=\-#*─—_~]{3,}\s*)?([^=\-#*─—_~\s][^\n]*?)\s*(?:[=\-#*─—_~]{3,})?\s*(?:\*/)?$")
_RULE = re.compile(r"^\s*(?:#|//|/\*+)\s*[=\-#*─—_~]{3,}")

_DEF_NODES = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "typescript": {"function_declaration": "function", "class_declaration": "class", "method_definition": "method",
                   "interface_declaration": "class", "type_alias_declaration": "type", "enum_declaration": "class"},
    "javascript": {"function_declaration": "function", "class_declaration": "class", "method_definition": "method"},
}
_DEF_NODES["tsx"] = _DEF_NODES["typescript"]
# inner nodes that name a region: (node type -> the field that holds its name)
_REGION_NODES = {
    "python": {"assignment": "left", "keyword_argument": "name", "pair": "key", "decorated_definition": ""},
    "typescript": {"variable_declarator": "name", "pair": "key", "assignment_expression": "left",
                   "public_field_definition": "name"},
    "javascript": {"variable_declarator": "name", "pair": "key", "assignment_expression": "left",
                   "field_definition": "property"},
}
_REGION_NODES["tsx"] = _REGION_NODES["typescript"]
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "lambda"}
_LANG_CACHE: Dict[str, Any] = {}


def lang_of(path: str) -> str:
    return LANG_BY_EXT.get(os.path.splitext(str(path or ""))[1].lower(), "")


def _language(lang: str):
    if lang in _LANG_CACHE:
        return _LANG_CACHE[lang]
    try:
        from tree_sitter import Language
        if lang == "python":
            import tree_sitter_python as m
            obj = Language(m.language())
        elif lang in ("typescript", "tsx"):
            import tree_sitter_typescript as m
            obj = Language(m.language_tsx() if lang == "tsx" else m.language_typescript())
        elif lang == "javascript":
            import tree_sitter_javascript as m
            obj = Language(m.language())
        else:
            obj = None
    except Exception:
        obj = None
    _LANG_CACHE[lang] = obj
    return obj


def parse(path: str, text: str):
    """``(tree, source bytes, lang)``, or None when the file is not code the parser knows."""
    lang = lang_of(path)
    language = _language(lang) if lang else None
    if language is None:
        return None
    from tree_sitter import Parser
    src = str(text or "").encode("utf-8", "replace")
    return Parser(language).parse(src), src, lang


def _text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _first_line(src: bytes, node, n: int = 80) -> str:
    return " ".join(_text(src, node).split("\n", 1)[0].split())[:n]


def _lines(node) -> Tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _value_of(node):
    for f in ("right", "value"):
        v = node.child_by_field_name(f)
        if v is not None:
            return v
    return None


def _callee(src: bytes, value) -> str:
    """What a region holds, in a few words: ``Stack(``, ``<Dialog>``, ``[...]``."""
    if value is None:
        return ""
    if value.type in ("call", "call_expression", "new_expression"):
        fn = value.child_by_field_name("function") or value.child_by_field_name("constructor")
        return (_first_line(src, fn, 60) if fn is not None else "") + "("
    if value.type in ("jsx_element", "jsx_self_closing_element", "parenthesized_expression"):
        return "<" + _first_line(src, value, 60).lstrip("(<").split(" ", 1)[0].split(">", 1)[0] + ">"
    if value.type in ("list", "array"):
        return "[...]"
    if value.type in ("dictionary", "object"):
        return "{...}"
    return ""


def outline(path: str, text: str) -> Optional[List[Dict[str, Any]]]:
    """The file's map: ``[{name, kind, start, end, depth}]`` in line order (definitions, their big
    inner regions, comment headers). None when the file is not code the parser knows."""
    parsed = parse(path, text)
    if parsed is None:
        return None
    tree, src, lang = parsed
    defs, regions = _DEF_NODES.get(lang, {}), _REGION_NODES.get(lang, {})
    out: List[Dict[str, Any]] = []

    def add(name: str, kind: str, node, depth: int) -> None:
        a, b = _lines(node)
        out.append({"name": name, "kind": kind, "start": a, "end": b, "depth": depth})

    stack = [(tree.root_node, 0, "", 0)]
    while stack:
        node, depth, owner, nest = stack.pop()     # nest: regions this node sits in
        kind = defs.get(node.type)
        child_depth, child_owner, child_nest = depth, owner, nest
        if kind:
            nm = node.child_by_field_name("name")
            name = _text(src, nm) if nm is not None else _first_line(src, node, 40)
            full = f"{owner}.{name}" if owner else name
            add(full, kind, node, depth)
            child_depth, child_owner, child_nest = depth + 1, full, 0
        elif node.type in regions and node.type != "decorated_definition":
            field = regions[node.type]
            nm = node.child_by_field_name(field) if field else None
            value = _value_of(node)
            a, b = _lines(node)
            if nm is not None and value is not None and value.type in _FUNCTION_VALUES and lang != "python":
                full = f"{owner}.{_text(src, nm)}" if owner else _text(src, nm)
                add(full, "function", node, depth)          # const Component = () => ...
                child_depth, child_owner, child_nest = depth + 1, full, 0
            elif nm is not None and b - a + 1 >= BIG_LINES * (1 + 2 * nest):
                # a region inside a region is named only when it is a large part of it
                label = " ".join(_text(src, nm).split())[:60]
                what = _callee(src, value)
                add(f"{owner}.{label}" if owner else label, "region", node, depth)
                out[-1]["holds"] = what
                child_depth, child_nest = depth + 1, nest + 1
        elif node.type in ("return_statement",) and owner:
            a, b = _lines(node)
            value = node.named_children[0] if node.named_children else None
            if value is not None and b - a + 1 >= BIG_LINES:
                add(f"{owner}.return", "region", node, depth)
                out[-1]["holds"] = _callee(src, value)
        elif node.type == "comment":
            line = _text(src, node).split("\n", 1)[0]
            prev = node.prev_sibling
            ruled = bool(_RULE.match(line)) or (prev is not None and prev.type == "comment"
                                               and bool(_RULE.match(_text(src, prev))))
            m = _HEADER.match(line)
            if ruled and m and m.group(1).strip() and not _RULE.fullmatch(line.strip()):
                add(m.group(1).strip()[:80], "section", node, depth)
        for ch in reversed(node.children):
            stack.append((ch, child_depth, child_owner, child_nest))
    out.sort(key=lambda d: (d["start"], d["depth"]))
    return out[:MAX_NODES]


def render_outline(path: str, nodes: List[Dict[str, Any]], total_lines: int) -> str:
    """The outline as the model reads it: one line per node, indented by depth."""
    head = f"OUTLINE of {path} ({total_lines} lines; read a part whole with read_symbol(path, name), "\
           "or read_file with offset/limit):"
    rows = [f"{'  ' * int(n['depth'])}{n['start']}-{n['end']} {n['kind']} {n['name']}"
            + (f" = {n['holds']}" if n.get("holds") else "") for n in nodes]
    return "\n".join([head] + rows)


_RANGE = re.compile(r"^L?(\d+)\s*[-:]\s*L?(\d+)$")


def find_symbol(nodes: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """The outline node ``name`` names: an exact full name, then the last dotted part, then case-blind."""
    want = str(name or "").strip()
    if not want:
        return None
    m = _RANGE.match(want)
    if m:
        return {"name": want, "kind": "lines", "start": int(m.group(1)), "end": int(m.group(2)), "depth": 0}
    for pick in (lambda n: n["name"] == want,
                 lambda n: n["name"].rsplit(".", 1)[-1] == want,
                 lambda n: n["name"].lower() == want.lower() or n["name"].rsplit(".", 1)[-1].lower() == want.lower()):
        hit = next((n for n in nodes if pick(n)), None)
        if hit is not None:
            return hit
    return None


def read_symbol(path: str, text: str, name: str) -> Optional[Dict[str, Any]]:
    """``{name, kind, start, end, body}`` of one node, whole -- in the shape of a read_file
    window, so the kernel remembers it as the file's exact lines. None when not found."""
    nodes = outline(path, text) or []
    hit = find_symbol(nodes, name)
    if hit is None:
        return None
    lines = str(text or "").split("\n")
    a, b = max(1, hit["start"]), min(len(lines), hit["end"])
    return {**hit, "start": a, "end": b, "body": "\n".join(lines[a - 1:b])}


def read_card(path: str, total_lines: int, a: int, b: int, body: str) -> str:
    """A part of a file in the read_file card form (``strip_file_card`` reads it back)."""
    return f"⟦ {path} · {total_lines} lines ⟧ [showing lines {a}-{b} of {total_lines}]\n{body}"


def syntax_errors(path: str, text: str, limit: int = 3) -> Optional[List[Tuple[int, int, str]]]:
    """The first ERROR / MISSING nodes: ``[(line, column, what)]``; [] when the file parses;
    None when the file is not code the parser knows."""
    parsed = parse(path, text)
    if parsed is None:
        return None
    tree, src, _lang = parsed
    if not tree.root_node.has_error:
        return []
    out: List[Tuple[int, int, str]] = []
    stack = [tree.root_node]
    while stack and len(out) < limit:
        node = stack.pop()
        if node.is_missing:
            out.append((node.start_point[0] + 1, node.start_point[1] + 1, f"missing {node.type!r}"))
            continue
        if node.type == "ERROR":
            out.append((node.start_point[0] + 1, node.start_point[1] + 1,
                        f"unexpected {_first_line(src, node, 40)!r}"))
            continue
        if node.has_error:
            stack.extend(reversed(node.children))
    return sorted(out)

