import os
import re
import subprocess
import time
from datetime import datetime


# Tools that MUTATE the workspace -> auto-checkpoint before each run
# (mirrors the kernel write tier; rollback snapshots itself, so not listed).
_WRITE_TIER = {"write_file", "edit_file", "multi_edit", "bash"}

# Informational stale-view warning appended to a SUCCESSFUL edit/write when
# the on-disk file is newer than the agent's last read (external edit under
# the agent). Never blocks the work -- the brain just learns to re-read.
_STALE_NOTE = "\n⚠ file changed on disk since you last read it — re-read to be safe"


def _relative_time(ts: float, now: float | None = None) -> str:
    """Compact human age for the read_file header: just now / 5m ago /
    3h ago / 2d ago / a calendar date past a week."""
    now = time.time() if now is None else now
    d = max(0.0, now - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 7 * 86400:
        return f"{int(d // 86400)}d ago"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class OutsideWorkspaceError(Exception):
    pass


class LocalToolExecutor:
    def __init__(self, workspace_root: str, indexer=None, shadow=None) -> None:
        self.root = os.path.realpath(workspace_root)
        self.indexer = indexer  # IntelService (or None on a base install) -- Task 5's _t_<verb> shims read this
        self.shadow = shadow    # ShadowGit (or None) -- the reversibility time machine
        # abs path -> st_mtime_ns of the file as the agent last SAW it (a
        # read_file, or this executor's own write) -- powers the stale-edit
        # warning. In-instance only: one executor == one coding session.
        self._read_mtimes: dict[str, int] = {}

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
        if (tool in _WRITE_TIER and self.shadow is not None
                and getattr(self.shadow, "auto_ok", True)):
            # The time machine snapshots BEFORE every mutation -- and must never
            # block or fail the actual work. A SINGLE transient failure must not
            # disable the session (final-review F8): note_auto_result latches
            # auto-checkpointing OFF only after several CONSECUTIVE failures; a
            # success re-enables it. Manual checkpoint/rollback always try.
            try:
                _cp = self.shadow.checkpoint(f"pre:{tool}")
                _note = getattr(self.shadow, "note_auto_result", None)
                if _note is not None:
                    _note(_cp is not None)
            except Exception:
                _note = getattr(self.shadow, "note_auto_result", None)
                if _note is not None:
                    _note(False)
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
        # The result travels to the brain as ONE json dict (kernel folds it
        # verbatim via _tool_result_content_str), so metadata rides in TWO
        # places: a compact bracketed header line PREPENDED to content (always
        # in the brain's view -- and, being first, it survives the kernel's
        # 50K-char tail truncation on big files) + structured fields for any
        # programmatic consumer. The file text after the header stays
        # byte-exact: edits re-read the DISK, so the header can never leak
        # into an old-string match.
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        st = os.stat(p)
        self._read_mtimes[p] = st.st_mtime_ns
        n = len(text.splitlines())
        header = self._file_header(os.path.relpath(p, self.root), n, st.st_mtime)
        return {"ok": True, "content": header + "\n" + text,
                "total_lines": n, "modified": int(st.st_mtime),
                "modified_iso": datetime.fromtimestamp(st.st_mtime)
                                        .astimezone().isoformat(timespec="seconds")}

    def _file_header(self, rel: str, total_lines: int, mtime: float) -> str:
        lines = "1 line" if total_lines == 1 else f"{total_lines} lines"
        parts = [rel, lines, f"modified {_relative_time(mtime)}"]
        parts.extend(self._intel_file_context(rel))
        return "⟦ " + " · ".join(parts) + " ⟧"

    def _intel_file_context(self, rel: str) -> list:
        """Graph FACTS for the read header -- what the file defines + which
        files use it, straight from the live repo index (IntelService.index /
        .graph). Best-effort and honest: the watcher swaps whole FileIndex /
        graph objects (never mutates them in place), so an unlocked read sees
        a consistent snapshot; a missing index, unindexed file, or any error
        degrades to [] (the header keeps lines+mtime only) -- never a crash,
        never an invented purpose the graph doesn't actually hold."""
        try:
            idx = getattr(self.indexer, "index", None)
            fi = idx.files.get(rel) if idx is not None else None
            if fi is None:
                return []
            names: list = []
            for kind in ("class", "function"):
                for s in fi.symbols:
                    if s.kind == kind and s.name not in names:
                        names.append(s.name)
            parts = []
            if names:
                extra = f" +{len(names) - 2} more" if len(names) > 2 else ""
                parts.append("defines " + ", ".join(names[:2]) + extra)
            graph = getattr(self.indexer, "graph", None)
            if graph is not None and fi.symbols:
                deps = sorted(graph.dependents_of({s.name for s in fi.symbols},
                                                  depth=1) - {rel})
                if deps:
                    extra = f" +{len(deps) - 3} more" if len(deps) > 3 else ""
                    parts.append("↔ used by " + ", ".join(deps[:3]) + extra)
            return parts
        except Exception:
            return []  # intel is advisory -- a read must never fail on it

    def _stale_note(self, p: str) -> str:
        """The stale-view warning, or "" -- fires only when the agent HAS a
        last-seen mtime for this file and the disk is strictly newer."""
        last = self._read_mtimes.get(p)
        try:
            return _STALE_NOTE if last is not None and os.stat(p).st_mtime_ns > last else ""
        except OSError:
            return ""

    def _note_own_write(self, p: str) -> None:
        """Our own successful write IS the agent's current view -- refresh the
        last-seen mtime so back-to-back edits don't false-alarm."""
        try:
            self._read_mtimes[p] = os.stat(p).st_mtime_ns
        except OSError:
            pass

    def _t_write_file(self, a: dict) -> dict:
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        stale = self._stale_note(p)
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(a.get("content", a.get("contents", "")))
        self._note_own_write(p)
        return {"ok": True, "content": f"wrote {rel}" + stale}

    def _t_edit_file(self, a: dict) -> dict:
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        stale = self._stale_note(p)
        old = a.get("old", a.get("old_string", ""))       # accept Claude-Code names
        new = a.get("new", a.get("new_string", ""))
        if not old:
            return {"ok": False, "content": "edit_file requires 'old' (the text to replace)"}
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        n = text.count(old)
        if n == 0:
            return {"ok": False, "content": "old string not found"}
        _ra = a.get("replace_all")
        replace_all = (_ra.strip().lower() in ("true", "1", "yes")
                       if isinstance(_ra, str) else bool(_ra))
        if n > 1 and not replace_all:
            return {"ok": False, "content":
                    f"old string occurs {n} times; add surrounding context to make it "
                    f"unique, or pass replace_all=true to replace every occurrence"}
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        self._note_own_write(p)
        note = f" ({n} occurrences)" if replace_all and n > 1 else ""
        return {"ok": True, "content": f"edited {rel}{note}" + stale}

    def _t_multi_edit(self, a: dict) -> dict:
        edits = a.get("edits")
        if not isinstance(edits, list) or not edits:
            return {"ok": False, "content": "multi_edit requires a non-empty 'edits' list"}
        # Validate EVERYTHING first -- all-or-nothing (a half-applied batch is
        # worse than a failed one).
        staged = []
        problems = []
        stale_rels = []           # checked BEFORE any write of this batch
        for i, e in enumerate(edits):
            if not isinstance(e, dict):
                problems.append(f"edit {i}: not an object")
                continue
            try:
                rel = self._rel(e)
                p = self.resolve_in_workspace(rel)
            except (ValueError, OutsideWorkspaceError) as err:
                problems.append(f"edit {i}: {err}")
                continue
            if self._stale_note(p) and rel not in stale_rels:
                stale_rels.append(rel)
            old = e.get("old", e.get("old_string", ""))
            new = e.get("new", e.get("new_string", ""))
            if not old:
                problems.append(f"edit {i} ({rel}): 'old' is required")
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError as err:
                problems.append(f"edit {i} ({rel}): {type(err).__name__}: {err}")
                continue
            n = text.count(old)
            if n != 1:
                problems.append(f"edit {i} ({rel}): 'old' occurs {n} times (must be exactly 1)")
                continue
            if not os.access(p, os.W_OK):
                problems.append(f"edit {i} ({rel}): file is not writable")
                continue
            staged.append((p, rel, old, new))
        if problems:
            return {"ok": False, "content":
                    "multi_edit applied NOTHING -- fix these and retry:\n" + "\n".join(problems)}
        # Apply sequentially, re-reading so multiple edits to the SAME file
        # compose; if an earlier edit invalidated a later one, stop honestly.
        applied = []
        for p, rel, old, new in staged:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
            if text.count(old) != 1:
                return {"ok": False, "content":
                        f"multi_edit stopped at {rel}: an earlier edit in this batch changed "
                        f"the text around 'old' (applied so far: {', '.join(applied) or 'none'}); "
                        f"re-read the file and retry the remaining edits"}
            with open(p, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new, 1))
            self._note_own_write(p)
            applied.append(rel)
        stale = ("\n⚠ changed on disk since you last read: " + ", ".join(stale_rels)
                 + " — re-read to be safe") if stale_rels else ""
        return {"ok": True,
                "content": f"applied {len(applied)} edits: " + ", ".join(applied) + stale}

    def _t_checkpoint(self, a: dict) -> dict:
        if self.shadow is None:
            return {"ok": False, "content": "reversibility is unavailable (no shadow git)"}
        cp = self.shadow.checkpoint(str(a.get("label", "") or "manual"))
        if cp is None:
            return {"ok": False, "content": "checkpoint failed (shadow git error)"}
        note = "created" if cp.get("changed") else "no changes since the last checkpoint"
        return {"ok": True, "content": f"checkpoint cp-{cp.get('n')} ({cp.get('id')}): {note}"}

    def _t_diff(self, a: dict) -> dict:
        if self.shadow is None:
            return {"ok": False, "content": "reversibility is unavailable (no shadow git)"}
        return {"ok": True, "content": self.shadow.diff(str(a.get("since", "") or ""))}

    def _t_rollback(self, a: dict) -> dict:
        if self.shadow is None:
            return {"ok": False, "content": "reversibility is unavailable (no shadow git)"}
        to = str(a.get("checkpoint", "") or a.get("to", "") or "")
        if not to:
            return {"ok": False, "content":
                    "rollback requires 'checkpoint' (an id, cp-N or N -- see diff/checkpoint output)"}
        return self.shadow.rollback(to)

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
