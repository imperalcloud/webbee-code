import os
import re
import subprocess


class OutsideWorkspaceError(Exception):
    pass


class LocalToolExecutor:
    def __init__(self, workspace_root: str, indexer=None) -> None:
        self.root = os.path.realpath(workspace_root)
        self.indexer = indexer  # IntelService (or None on a base install) -- Task 5's _t_<verb> shims read this

    def resolve_in_workspace(self, path: str) -> str:
        full = os.path.realpath(os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise OutsideWorkspaceError(path)
        return full

    def run(self, tool: str, args: dict) -> dict:
        # Some providers deliver tool arguments as a JSON string, not a dict.
        if isinstance(args, str):
            import json
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        try:
            fn = getattr(self, f"_t_{tool}", None)
            if fn is None:
                return {"ok": False, "content": f"unknown tool: {tool}"}
            return fn(args)
        except OutsideWorkspaceError as e:
            # Return it as a normal tool result (NEVER re-raise): a re-raise
            # escaped run(), the reverse-channel handler never posted a result,
            # and the kernel hung waiting -> the whole turn/dock froze. The brain
            # sees the message and adapts (e.g. stays inside the workspace).
            return {"ok": False, "content":
                    f"path is outside the workspace and cannot be accessed: {e}"}
        except Exception as e:  # surface tool errors to the brain, don't crash
            return {"ok": False, "content": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _rel(a: dict) -> str:
        """The file path from whichever key the brain used. Models vary: Claude
        emits file_path/old_string, GPT may use other names — accept the common
        ones, then ANY key that mentions path/file, then fail with a clear error
        that ECHOES the keys we DID get (so a stubborn model's shape is visible)."""
        for k in ("path", "file_path", "filepath", "filename", "file",
                  "target_file", "target_path", "target", "name"):
            v = a.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for k, v in a.items():                       # fuzzy: any *path*/*file* key
            if isinstance(v, str) and v.strip() and ("path" in k.lower() or "file" in k.lower()):
                return v
        raise ValueError(f"'path' argument is missing (got keys: {sorted(a.keys())})")

    def _t_read_file(self, a: dict) -> dict:
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        with open(p, "r", encoding="utf-8") as f:
            return {"ok": True, "content": f.read()}

    def _t_write_file(self, a: dict) -> dict:
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(a.get("content", a.get("contents", "")))
        return {"ok": True, "content": f"wrote {rel}"}

    def _t_edit_file(self, a: dict) -> dict:
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        old = a.get("old", a.get("old_string", ""))       # accept Claude-Code names
        new = a.get("new", a.get("new_string", ""))
        if not old:
            return {"ok": False, "content": "edit_file requires 'old' (the text to replace)"}
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        if old not in text:
            return {"ok": False, "content": "old string not found"}
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new, 1))
        return {"ok": True, "content": f"edited {rel}"}

    def _t_bash(self, a: dict) -> dict:
        timeout = min(int(a.get("timeout", 120) or 120), 3600)
        proc = subprocess.run(
            a["command"], shell=True, cwd=self.root,
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "content": out or f"(exit {proc.returncode})"}

    def _t_grep(self, a: dict) -> dict:
        pat = re.compile(a["pattern"])
        base = self.resolve_in_workspace(a.get("path", "."))
        hits = []
        for dp, _dn, fns in os.walk(base):
            if "/.git" in dp:
                continue
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f, 1):
                            if pat.search(line):
                                rel = os.path.relpath(fp, self.root)
                                hits.append(f"{rel}:{i}:{line.rstrip()}")
                except (UnicodeDecodeError, OSError):
                    continue
        return {"ok": True, "content": "\n".join(hits[:200]) or "(no matches)"}

    def _t_glob(self, a: dict) -> dict:
        import glob as _g
        base = os.path.join(self.root, a["pattern"])
        rels = [os.path.relpath(p, self.root) for p in _g.glob(base, recursive=True)]
        return {"ok": True, "content": "\n".join(sorted(rels)) or "(no matches)"}

    def _t_repo_profile(self, a: dict) -> dict:
        return self._cpc("repo_profile", a)

    def _t_graph_slice(self, a: dict) -> dict:
        return self._cpc("graph_slice", a)

    def _t_search_code(self, a: dict) -> dict:
        return self._cpc("search_code", a)

    def _t_impact_of_change(self, a: dict) -> dict:
        return self._cpc("impact_of_change", a)

    def _t_orient(self, a: dict) -> dict:
        return self._cpc("orient", a)

    @staticmethod
    def _as_str_list(v):
        """Coerce `symbols` into a list[str]. An "any LLM" surface may emit a
        bare string ("beta") or a stringified JSON array ('["beta"]') instead
        of a real list -- fed straight to query.graph_slice/impact_of_change,
        `for name in symbols` iterates characters and silently returns
        total:0 (a false negative the brain reads as "no callers")."""
        import json as _j
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    p = _j.loads(s)
                    if isinstance(p, list):
                        return [str(x) for x in p]
                except Exception:
                    pass
            return [s] if s else []
        return []

    def _cpc(self, verb: str, a: dict) -> dict:
        if self.indexer is None:
            return {"ok": False, "content": "intel not available; install webbee[intel]"}
        from webbee.intel import query
        if verb == "repo_profile":
            return query.repo_profile(self.indexer)
        if verb == "graph_slice":
            return query.graph_slice(self.indexer, self._as_str_list(a.get("symbols")), int(a.get("depth", 1) or 1))
        if verb == "search_code":
            return query.search_code(self.indexer, a.get("query", ""), int(a.get("k", 20) or 20),
                                      a.get("kind"), a.get("path_glob"))
        if verb == "impact_of_change":
            return query.impact_of_change(self.indexer, self._as_str_list(a.get("symbols")))
        if verb == "orient":
            return query.orient(self.indexer, a.get("query", ""))
        return {"ok": False, "content": f"unknown cpc verb: {verb}"}
