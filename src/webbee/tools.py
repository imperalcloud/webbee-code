import os
import re
import subprocess


class OutsideWorkspaceError(Exception):
    pass


class LocalToolExecutor:
    def __init__(self, workspace_root: str) -> None:
        self.root = os.path.realpath(workspace_root)

    def resolve_in_workspace(self, path: str) -> str:
        full = os.path.realpath(os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise OutsideWorkspaceError(path)
        return full

    def run(self, tool: str, args: dict) -> dict:
        try:
            fn = getattr(self, f"_t_{tool}", None)
            if fn is None:
                return {"ok": False, "content": f"unknown tool: {tool}"}
            return fn(args)
        except OutsideWorkspaceError:
            raise
        except Exception as e:  # surface tool errors to the brain, don't crash
            return {"ok": False, "content": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _rel(a: dict) -> str:
        """The file path from whichever key the brain used. Claude-family models
        default to Claude-Code names (file_path), so accept the common synonyms;
        a clean ValueError (not KeyError) lets the brain self-correct."""
        raw = a.get("path") or a.get("file_path") or a.get("filename") or a.get("file")
        if not raw:
            raise ValueError("required 'path' argument is missing")
        return raw

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
        proc = subprocess.run(
            a["command"], shell=True, cwd=self.root,
            capture_output=True, text=True, timeout=a.get("timeout", 120),
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
