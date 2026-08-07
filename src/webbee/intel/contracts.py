"""Deterministic endpoint/schema evidence extraction.

This is deliberately a projection over source snapshots, not a second source
of truth. Every record carries file+line provenance and is rebuilt from the
content-addressed repository index. Unsupported syntax is simply absent; no
LLM inference is stored as fact.
"""
from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from typing import Iterable

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})


@dataclass(frozen=True)
class EndpointEvidence:
    method: str
    route: str
    handler: str
    path: str
    line: int
    schema_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SchemaEvidence:
    name: str
    schema_kind: str
    path: str
    line: int
    fields: tuple[str, ...] = ()
    storage_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = _name(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    if isinstance(node, ast.Subscript):
        return _name(node.value)
    return ""


def _literal_string(node) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _annotation_names(node) -> set[str]:
    if node is None:
        return set()
    names = set()
    for part in ast.walk(node):
        n = _name(part)
        if n and n.split(".")[-1][:1].isupper():
            names.add(n.split(".")[-1])
    return names


def _python_contracts(path: str, text: str):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], []
    endpoints, schemas = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                method = dec.func.attr.lower()
                route = _literal_string(dec.args[0]) if dec.args else ""
                if method not in _HTTP_METHODS or not route.startswith("/"):
                    continue
                refs = set()
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    refs.update(_annotation_names(arg.annotation))
                refs.update(_annotation_names(node.returns))
                for kw in dec.keywords:
                    if kw.arg in {"response_model", "request_model", "model"}:
                        refs.update(_annotation_names(kw.value))
                endpoints.append(EndpointEvidence(
                    method.upper(), route, node.name, path,
                    getattr(dec, "lineno", node.lineno), tuple(sorted(refs))))
        elif isinstance(node, ast.ClassDef):
            bases = {_name(b).split(".")[-1] for b in node.bases}
            is_model = bool(bases & {"BaseModel", "TypedDict", "Schema", "Serializer"})
            is_table = bool(bases & {"Base", "DeclarativeBase", "Model"})
            if not (is_model or is_table):
                continue
            fields, storage = set(), ""
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
                elif isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            if target.id == "__tablename__":
                                storage = _literal_string(stmt.value)
                            elif isinstance(stmt.value, ast.Call):
                                call = _name(stmt.value.func).split(".")[-1]
                                if call in {"Column", "mapped_column", "Field"}:
                                    fields.add(target.id)
            schemas.append(SchemaEvidence(
                node.name, "table" if is_table else "model", path, node.lineno,
                tuple(sorted(fields)), storage))
    return endpoints, schemas


_TS_ENDPOINT = re.compile(
    r"(?m)\b(?:app|router|server)\s*\.\s*(get|post|put|patch|delete|options|head)"
    r"\s*\(\s*([\"'])(/[^\"']*)\2\s*,\s*([A-Za-z_$][\w$]*)"
)
_TS_SCHEMA = re.compile(
    r"(?ms)\b(?:export\s+)?(?:interface|type|class)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s+extends[^\{=]+)?\s*(?:\{|=\s*\{)(.*?)\}"
)
_TS_FIELD = re.compile(r"(?m)^\s*([A-Za-z_$][\w$]*)\s*\??\s*:")


def _typescript_contracts(path: str, text: str):
    endpoints = [EndpointEvidence(
        m.group(1).upper(), m.group(3), m.group(4), path,
        text.count("\n", 0, m.start()) + 1)
        for m in _TS_ENDPOINT.finditer(text)]
    schemas = []
    for m in _TS_SCHEMA.finditer(text):
        schemas.append(SchemaEvidence(
            m.group(1), "model", path, text.count("\n", 0, m.start()) + 1,
            tuple(sorted(set(_TS_FIELD.findall(m.group(2)))))))
    return endpoints, schemas


def extract_contracts(path: str, text: str, lang: str):
    if lang == "python":
        return _python_contracts(path, text)
    if lang in {"typescript", "javascript"}:
        return _typescript_contracts(path, text)
    return [], []


def match_contracts(repos: Iterable[dict]) -> list[dict]:
    """Build deterministic cross-repo links from exact contract identities.

    Endpoint route+method and schema names are intentionally exact. Semantic
    guesses belong in retrieval ranking, never in the evidence graph.
    """
    repos = sorted(repos, key=lambda r: r.get("repo_key", ""))
    out = []
    for i, left in enumerate(repos):
        for right in repos[i + 1:]:
            lk, rk = left.get("repo_key", ""), right.get("repo_key", "")
            le = {(x.method, x.route): x for x in left.get("endpoints", ())}
            re_ = {(x.method, x.route): x for x in right.get("endpoints", ())}
            for key in sorted(le.keys() & re_.keys()):
                out.append({"relation": "endpoint_identity", "source_repo": lk,
                            "target_repo": rk, "identity": f"{key[0]} {key[1]}"})
            # A schema contract can be declared as a concrete model/table OR
            # referenced by an endpoint annotation/response_model. Both are
            # deterministic source evidence; including references lets a
            # producer endpoint link to a consumer's shared DTO even when the
            # producer imports that DTO from another package.
            def schema_names(repo):
                names = {x.name for x in repo.get("schemas", ())}
                for endpoint in repo.get("endpoints", ()):
                    names.update(endpoint.schema_refs)
                return names

            for name in sorted(schema_names(left) & schema_names(right)):
                source, target = sorted((lk, rk))
                out.append({"relation": "schema_name_match", "source_repo": source,
                            "target_repo": target, "identity": name})
    return out
