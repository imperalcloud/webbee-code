from __future__ import annotations
import os

from webbee.intel.models import FileIndex, ProjectIndex, Symbol

LANG_BY_EXT = {".py": "python", ".ts": "typescript", ".tsx": "typescript",
               ".js": "javascript", ".jsx": "javascript"}

# tree-sitter Language handles are lazy-loaded + cached (import cost is real).
_LANG_CACHE: dict = {}


def _get_language(lang: str):
    if lang in _LANG_CACHE:
        return _LANG_CACHE[lang]
    try:
        if lang == "python":
            import tree_sitter_python as ts_py
            from tree_sitter import Language
            obj = Language(ts_py.language())
        elif lang == "typescript":
            import tree_sitter_typescript as ts_ts
            from tree_sitter import Language
            obj = Language(ts_ts.language_typescript())
        elif lang == "javascript":
            import tree_sitter_javascript as ts_js
            from tree_sitter import Language
            obj = Language(ts_js.language())
        else:
            obj = None
    except Exception:
        obj = None
    _LANG_CACHE[lang] = obj
    return obj


# Minimal per-language node-type -> symbol-kind maps (clean-room; NOT the AGPL
# package's queries). We walk the tree once and pick definition nodes; this is
# syntax-level extraction, exactly what tree-sitter guarantees.
_DEF_NODES = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "typescript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method", "interface_declaration": "class"},
    "javascript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method"},
}


def _lang_for(path: str) -> str:
    return LANG_BY_EXT.get(os.path.splitext(path)[1].lower(), "other")


def _name_of(node, src_bytes: bytes) -> str:
    n = node.child_by_field_name("name")
    if n is not None:
        return src_bytes[n.start_byte:n.end_byte].decode("utf-8", "replace")
    return ""


def parse_file(path: str, text: str) -> FileIndex | None:
    lang = _lang_for(path)
    fi = FileIndex(path=path, lang=lang)
    if lang == "other":
        return fi  # line-only fallback (no symbols) — never crash
    L = _get_language(lang)
    if L is None:
        return fi
    try:
        from tree_sitter import Parser
        parser = Parser(L)
        src = text.encode("utf-8", "replace")
        tree = parser.parse(src)
        defmap = _DEF_NODES.get(lang, {})
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            kind = defmap.get(node.type)
            if kind:
                # A def/class node's own name-identifier must NOT be walked
                # as a generic child -- otherwise it lands in `refs` too,
                # making the defining file its own "caller"/"dependent".
                # Nested defs (methods, inner functions) are still reached:
                # every other child is still pushed onto the stack.
                # NOTE: tree-sitter Node objects are re-wrapped on each
                # access, so `is`/`is not` identity checks don't hold across
                # two separate child_by_field_name/children calls -- compare
                # by (start_byte, end_byte) span instead.
                name_node = node.child_by_field_name("name")
                name_span = (name_node.start_byte, name_node.end_byte) if name_node is not None else None
                nm = _name_of(node, src)
                if nm:
                    sig = src[node.start_byte:min(node.end_byte, node.start_byte + 200)].decode("utf-8", "replace").split("\n", 1)[0]
                    fi.symbols.append(Symbol(name=nm, kind=kind, path=path,
                                             start_line=node.start_point[0] + 1,
                                             end_line=node.end_point[0] + 1, signature=sig))
                for ch in node.children:
                    if name_span is None or (ch.start_byte, ch.end_byte) != name_span:
                        stack.append(ch)
                continue
            if node.type in ("identifier", "call", "call_expression"):
                fi.refs.append(src[node.start_byte:node.end_byte].decode("utf-8", "replace").split("(")[0].strip())
            stack.extend(node.children)
    except Exception:
        return fi  # parse error -> line-only, fail-soft
    return fi


def build_index(root: str, files: list[str]) -> ProjectIndex:
    idx = ProjectIndex()
    for rel in files:
        try:
            with open(os.path.join(root, rel), "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        fi = parse_file(rel, text)
        if fi is not None:
            idx.files[rel] = fi
    return idx
