import os
import re
import subprocess
import threading
import time
from datetime import datetime


# Tools that MUTATE the workspace -> auto-checkpoint before each run
# (mirrors the kernel write tier; rollback snapshots itself, so not listed).
_WRITE_TIER = {"write_file", "edit_file", "multi_edit", "shell", "bash"}
# The tools plan mode runs -- the ones that only read (an allowlist: a checkpoint, a rollback, a command, an edit or
# any tool added later is refused in plan mode until it is named here -- security review 2026-10-30)
PLAN_SAFE_TOOLS = frozenset({
    "read_file", "read_symbol", "outline", "list_dir", "glob", "grep", "search_code", "orient", "repo_profile",
    "graph_slice", "impact_of_change", "contract_slice", "diff", "read_url", "web_search", "view_image",
    "inspect_page", "intel_sync_local", "intel_sync_remote",
})

# Informational stale-view warning appended to a SUCCESSFUL edit/write when
# the on-disk file is newer than the agent's last read (external edit under
# the agent). Never blocks the work -- the brain just learns to re-read.
_STALE_NOTE = "\n⚠ file changed on disk since you last read it — re-read to be safe"

# Every successful write_file/edit_file/multi_edit computes a unified diff
# (0.3.40, I-STREAM-STEP-DIFF): cheap (difflib on two already-in-memory
# strings, no subprocess, no extra disk I/O beyond the write that already
# happened) and capped so a rewritten 50k-line file can never blow up a
# frame or the terminal -- the point is "what changed", not "the whole file
# again". A binary/undecodable file (caught by the caller's own try/except)
# never reaches this -- no diff is produced rather than a garbled one.
_DIFF_CAP = 4000

# Output discipline (I-TOOLS-HONEST-TRUNCATION): EVERY tool that can emit an
# unbounded amount of text caps it AND says so. Silence is the bug -- a capped
# result that looks complete makes the agent conclude "that's all there is"
# and reason from a half-truth. Measured before this existed: `bash` returned
# 200,001 chars uncapped (one `cat` of a big log burned the context), and
# `grep` returned exactly 200 of 500 real matches with NO marker at all.
_BASH_OUT_CAP = 30000          # chars of combined stdout+stderr
_GLOB_MAX = 2000               # paths a glob answers with (the rest counted)
_GREP_HIT_CAP = 200            # matching lines
_READ_LINE_CAP = 2000          # lines per read_file call without explicit limit
# A code file longer than this, read without a window, answers with its OUTLINE first
# (CPC outline/read_symbol): live, a 1459-line declarative screen was read in 40-120
# line windows ~190 times. The outline names every part with its lines; read_symbol
# reads one part whole; offset/limit still reads any window (limit reads it whole).
_OUTLINE_FIRST_LINES = 600


def _truncated(text: str, cap: int, unit: str = "chars") -> str:
    """PURE. Cap `text`, appending an HONEST marker when anything was dropped.
    Keeps the HEAD (where errors and headers live) and the TAIL (where summaries live) and states how much is gone
    so the agent can decide to narrow the command instead of assuming it saw
    everything."""
    t = text or ""
    if len(t) <= cap:
        return t
    dropped = len(t) - cap
    # the head AND the tail (2026-10-30 review: a test run, a build, a compiler print their summary at the END --
    # kept only by its head, a long pytest run never showed which tests failed)
    head, tail = cap // 3, cap - cap // 3
    return (t[:head] +
            f"\n\n… ⚠ TRUNCATED: {dropped:,} of {len(t):,} {unit} hidden here -- showing the first {head:,} and the "
            f"last {tail:,}. Narrow the command (head/tail/grep) to see the middle.\n\n" + t[-tail:])


def _norm(text: str, eol: str) -> str:
    """``text`` in the file's own line endings' working form: a CRLF file is edited as ``\n`` text."""
    return text.replace("\r\n", "\n") if eol == "\r\n" else text


def _read_text(p: str) -> tuple:
    """``(text, eol)`` of a file read with its line endings kept (2026-10-30 review: a text-mode read and write turned a
    CRLF file into LF -- or an LF file into CRLF on Windows -- whole, on every edit, while the diff showed one line):
    a file whose every line ends in CRLF is worked on as ``\n`` text and written back in CRLF; any other file as it
    is."""
    with open(p, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = raw.count("\r\n")
    eol = "\r\n" if crlf and crlf == raw.count("\n") else "\n"
    return _norm(raw, eol), eol


def _write_text(p: str, text: str, eol: str = "\n") -> None:
    """Write ``text`` (``\n`` lines) in ``eol`` line endings, atomically: a temporary file beside it, then a rename
    (2026-10-30 review: open "w" truncated first -- a crash or a full disk left a cut source file). The file's mode
    is kept."""
    import tempfile
    data = _norm(text, eol).replace("\n", "\r\n") if eol == "\r\n" else text
    d = os.path.dirname(p) or "."
    fd, tmp = tempfile.mkstemp(prefix=".webbee-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
        try:
            os.chmod(tmp, os.stat(p).st_mode & 0o7777)
        except OSError:
            pass                          # a new file: the default mode
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stop_group(proc, posix: bool) -> None:
    """Stop a command and every process it started: SIGTERM to its group, SIGKILL 3 s later (Windows: kill it)."""
    import signal
    try:
        if posix:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def _decoded(raw: bytes) -> str:
    """A command's output as text: UTF-8 -- with a stray invalid byte replaced, never the whole output read in a legacy
    code page (2026-10-30 review: one bad byte turned Cyrillic UTF-8 into cp1251 mojibake); a legacy code page only
    when the bytes are mostly not UTF-8 (a Windows tool printing cp1251 / cp866)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    text = raw.decode("utf-8", errors="replace")
    if text.count("\ufffd") <= max(4, len(text) // 100):
        return text
    for enc in ("cp1251", "cp866", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return text


def _unified_diff(rel: str, before: str, after: str) -> str:
    """PURE. before == after -> "" (a no-op edit shows no diff, not an empty
    one). Otherwise a standard unified diff (3 lines of context, like `git
    diff`), truncated at `_DIFF_CAP` chars with an honest marker -- never
    silently dropped, never large enough to flood the transcript."""
    if before == after:
        return ""
    from difflib import unified_diff
    lines = list(unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=rel, tofile=rel, n=3))
    out = "".join(lines)
    if len(out) > _DIFF_CAP:
        out = out[:_DIFF_CAP] + f"\n… (diff truncated at {_DIFF_CAP} chars)\n"
    return out


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


def _is_git_dir(dp: str) -> bool:
    """PURE. True if dp is (or is under) a .git directory. Normalized to
    forward slashes so the skip works on Windows too, where os.walk yields
    backslash paths (an un-normalized "/.git" check never matched there,
    so grep walked .git's object store on every call)."""
    p = dp.replace(os.sep, "/").replace("\\", "/")
    return "/.git" in p


class OutsideWorkspaceError(Exception):
    pass


NEAREST_CONTEXT = 5        # lines shown around the closest match of an edit's 'old' that was not found


def nearest_text(text: str, old: str) -> str:
    """Where ``old`` comes closest in ``text`` when it is not there as written: the file's current lines around it,
    numbered, with how close it is and the first line that differs -- so the next edit is written from the file, not
    from memory (live 2026-10-30: 37% of edit_file calls in a day failed "old string not found", each followed by a
    re-read and a guess). Whitespace-only differences are named as such. "" -- nothing comes close."""
    import difflib
    lines = text.split("\n")
    want = old.split("\n")
    n = max(1, len(want))
    if not lines or not old.strip():
        return ""
    norm = lambda s: " ".join(s.split())          # noqa: E731
    flat = [norm(x) for x in lines]
    target = "\n".join(norm(w) for w in want)
    # candidate starts: each line of 'old' found in the file votes for where 'old' would begin; with none found, the
    # file lines closest to its longest line -- only the best candidates are scored (a 10 000-line file in ~0.1 s)
    where: dict = {}
    for j, w in enumerate(norm(x) for x in want):
        if w:
            for i, x in enumerate(flat):
                if x == w:
                    where[i - j] = where.get(i - j, 0) + 1
    if not where:
        longest = max(want, key=len)
        near = difflib.get_close_matches(norm(longest), flat, n=10, cutoff=0.5)
        j = want.index(longest)
        for i, x in enumerate(flat):
            if x in near:
                where[i - j] = where.get(i - j, 0) + 1
    starts = sorted(where, key=lambda k: -where[k])[:20]
    best, at = 0.0, 0
    for i in starts:
        i = max(0, min(i, max(0, len(lines) - n)))
        r = difflib.SequenceMatcher(None, "\n".join(flat[i:i + n]), target, autojunk=False).ratio()
        if r > best:
            best, at = r, i
    if best < 0.5:
        return ""
    lo, hi = max(0, at - NEAREST_CONTEXT), min(len(lines), at + n + NEAREST_CONTEXT)
    shown = "\n".join(f"{k + 1:6d}| {lines[k]}" for k in range(lo, hi))
    if best == 1.0:
        head = (f"the text is at line {at + 1} with different whitespace (spaces, tabs or line ends) -- copy it from "
                "these lines exactly")
    else:
        diff = next((k for k in range(n) if at + k < len(lines) and k < len(want)
                     and lines[at + k] != want[k]), 0)
        head = (f"the closest place is line {at + 1} ({int(best * 100)}% alike); the first line that differs is "
                f"line {at + diff + 1}: the file has {lines[at + diff][:160]!r}, your 'old' has {want[diff][:160]!r}"
                if at + diff < len(lines) and diff < len(want) else f"the closest place is line {at + 1}")
    return f"{head}. The file now (lines {lo + 1}-{hi}):\n{shown}"


class LocalToolExecutor:
    def __init__(self, workspace_root: str, indexer=None, shadow=None,
                 client_factory=None) -> None:
        self.root = os.path.realpath(workspace_root)
        self.indexer = indexer  # IntelService (or None on a base install) -- Task 5's _t_<verb> shims read this
        self.shadow = shadow    # ShadowGit (or None) -- the reversibility time machine
        if indexer is not None and client_factory is not None and hasattr(indexer, "attach_vault"):
            # code graph everywhere: the workspace's index is a source of the vault's one graph
            indexer.attach_vault(client_factory)
        # Zero-arg callable -> ImperalClient, used ONLY to reach the system
        # file-reader extension when read_file meets a document or an image
        # (native-document-read-v1). A factory, not a client, so a session with
        # no cloud reach simply has None and that path degrades to one honest
        # sentence instead of a crash.
        self.client_factory = client_factory
        # abs path -> st_mtime_ns of the file as the agent last SAW it (a
        # read_file, or this executor's own write) -- powers the stale-edit
        # warning. In-instance only: one executor == one coding session.
        self._read_mtimes: dict[str, int] = {}
        # the commands running now (bash): a stop (Esc) ends them and every process they started (cancel_running)
        self._running: set = set()
        self._running_lock = threading.Lock()

    def cancel_running(self) -> int:
        """Stop every command this executor is running now and every process it started (2026-10-30 review: Esc
        cancelled the turn, while the command -- a deploy, a migration -- ran on in its thread for up to an hour).
        Returns how many were stopped."""
        with self._running_lock:
            procs = [p for p in self._running if p.poll() is None]
        for p in procs:
            _stop_group(p, os.name != "nt")
        return len(procs)

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
        if self.indexer is not None and hasattr(self.indexer, "sync_if_due"):
            try:            # the cloud code map kept fresh while the session works -- never in the call's way
                self.indexer.sync_if_due()
            except Exception:
                pass
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

        # native-document-read-v1: a .pdf/.docx/.xlsx/.odt or an image is NOT
        # broken input -- it is a document the platform can already read. Before
        # this, opening one as UTF-8 raised UnicodeDecodeError and the agent got
        # nothing at all. Route it to the system File Reader instead; source
        # files take the byte-exact text path below, untouched.
        from webbee.native_files import is_native
        if is_native(rel):
            from webbee.native_read import read_native
            return read_native(self.client_factory, p, rel,
                               offset=int(a.get("offset") or 0),
                               limit=int(a.get("limit") or 0))

        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            # A binary whose extension did not give it away (no extension, an
            # unusual one, a mislabelled export). Same door rather than the
            # decode error the agent could do nothing with.
            from webbee.native_read import read_native
            return read_native(self.client_factory, p, rel,
                               offset=int(a.get("offset") or 0),
                               limit=int(a.get("limit") or 0))
        st = os.stat(p)
        self._read_mtimes[p] = st.st_mtime_ns
        lines = text.splitlines()
        n = len(lines)

        # Partial reads (I-TOOLS-PARTIAL-READ): before this, read_file was
        # all-or-nothing, so inspecting lines 400-450 of a 3000-line file
        # dragged the whole file through the context -- the reason the agent
        # kept reaching for `sed -n` via bash instead of its own tool.
        # 1-based, inclusive `offset`; `limit` lines from there.
        try:
            offset = int(a.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(a.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        partial = offset > 0 or limit > 0

        start = max(offset - 1, 0) if offset > 0 else 0
        if start > n:
            start = n
        end = (start + limit) if limit > 0 else n
        # Even without an explicit window, never dump an enormous file blind.
        if not partial and n > _READ_LINE_CAP:
            end = _READ_LINE_CAP
        end = min(end, n)

        if not partial and n > _OUTLINE_FIRST_LINES:
            first = self._outline_first(os.path.relpath(p, self.root), text, n, st)
            if first is not None:
                return first

        # Byte-exact when the whole file is returned: splitlines()+join drops a
        # trailing newline, which would corrupt edit_file's exact-match contract
        # and rewrite files without their final newline. Only a genuine WINDOW
        # is re-joined; a full read hands back the original text untouched.
        if start == 0 and end == n:
            body = text
        else:
            body = "\n".join(lines[start:end])
        header = self._file_header(os.path.relpath(p, self.root), n, st.st_mtime)
        if start or end < n:
            header += (f" [showing lines {start + 1}-{end} of {n}"
                       + ("" if partial else f"; auto-capped at {_READ_LINE_CAP} — "
                                             f"pass offset/limit for the rest")
                       + "]")
        return {"ok": True, "content": header + "\n" + body,
                "shown_from": start + 1, "shown_to": end,
                "truncated": (start > 0 or end < n),
                "total_lines": n, "modified": int(st.st_mtime),
                "modified_iso": datetime.fromtimestamp(st.st_mtime)
                                        .astimezone().isoformat(timespec="seconds")}

    def _outline_first(self, rel: str, text: str, n: int, st) -> dict | None:
        """The OUTLINE answer to a whole read of a big code file, or None (not code / no parser /
        too few parts to be a map). Never the read_file card: the kernel must not take the map
        for the file's lines."""
        from webbee.intel import outline as ol
        try:
            nodes = ol.outline(rel, text)
        except Exception:
            nodes = None
        if not nodes or len(nodes) < 3:
            return None
        return {"ok": True, "content": ol.render_outline(rel, nodes, n)
                + f"\n(read_file with offset/limit reads any window; limit={n} reads the whole file)",
                "outline": True, "total_lines": n, "modified": int(st.st_mtime)}

    def _code_text(self, a: dict):
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        with open(p, "r", encoding="utf-8") as f:
            return os.path.relpath(p, self.root), f.read()

    def _t_outline(self, a: dict) -> dict:
        from webbee.intel import outline as ol
        try:
            rel, text = self._code_text(a)
        except (OSError, UnicodeDecodeError) as e:
            return {"ok": False, "content": f"outline: cannot read {a.get('path')}: {e}"}
        nodes = ol.outline(rel, text)
        if nodes is None:
            return {"ok": False, "content": f"outline: {rel} is not Python/TS/JS code (or the parser is "
                                            "not installed) -- read it with read_file"}
        return {"ok": True, "content": ol.render_outline(rel, nodes, len(text.split("\n"))),
                "nodes": nodes}

    def _t_read_symbol(self, a: dict) -> dict:
        from webbee.intel import outline as ol
        try:
            rel, text = self._code_text(a)
        except (OSError, UnicodeDecodeError) as e:
            return {"ok": False, "content": f"read_symbol: cannot read {a.get('path')}: {e}"}
        part = ol.read_symbol(rel, text, str(a.get("name") or ""))
        if part is None:
            return {"ok": False, "content": f"read_symbol: no part named {a.get('name')!r} in {rel} -- "
                                            "outline(path) lists them"}
        p = self.resolve_in_workspace(rel)
        self._read_mtimes[p] = os.stat(p).st_mtime_ns
        total = len(text.split("\n"))
        return {"ok": True, "content": ol.read_card(rel, total, part["start"], part["end"], part["body"]),
                "shown_from": part["start"], "shown_to": part["end"], "total_lines": total,
                "symbol": part["name"], "kind": part["kind"]}

    def _t_inspect_page(self, a: dict) -> dict:
        """The page in the local browser (CDP, port 9222): boxes, roles and colors per width and a
        screenshot -- the kernel measures them (truth-loop stage 10, webbee.browser)."""
        from webbee import browser
        widths = a.get("widths") if isinstance(a.get("widths"), list) else None
        return browser.inspect_page(url=str(a.get("url") or ""), tab=str(a.get("tab") or ""), widths=widths,
                                    screenshot=a.get("screenshot", True) is not False,
                                    port=int(os.environ.get("WEBBEE_CDP_PORT") or browser.DEFAULT_PORT))

    def _reindex(self, rel: str) -> None:
        """A file this executor wrote is re-indexed at once (CPC freshness): the code map never
        answers from the text before the agent's own change. Advisory -- never fails a write."""
        try:
            if self.indexer is not None and getattr(self.indexer, "ready", False):
                # the index's root may be above the launch folder (Webbee started in a sub-folder of the repo,
                # 2026-10-30 review): the path is the index's, never the launch folder's
                iroot = getattr(self.indexer, "root", "") or self.root
                rel = os.path.relpath(os.path.join(self.root, rel), iroot)
                if not rel.startswith(".."):
                    self.indexer.apply_changes([rel])
        except Exception:
            pass

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
        before, eol = "", "\n"
        try:
            before, eol = _read_text(p)     # an existing CRLF file stays CRLF
        except OSError:
            pass          # new file -- diff against "" (a pure addition)
        after = a.get("content", a.get("contents", ""))
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        if eol == "\r\n":
            after = _norm(after, eol)
        _write_text(p, after, eol)
        self._note_own_write(p)
        self._reindex(os.path.relpath(p, self.root))
        out = {"ok": True, "content": f"wrote {rel}" + stale}
        diff = _unified_diff(rel, before, after)
        if diff:
            out["diff"] = diff
        return out

    def _t_edit_file(self, a: dict) -> dict:
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)
        stale = self._stale_note(p)
        old = a.get("old", a.get("old_string", ""))       # accept Claude-Code names
        new = a.get("new", a.get("new_string", ""))
        if not old:
            return {"ok": False, "content": "edit_file requires 'old' (the text to replace)"}
        text, eol = _read_text(p)
        old, new = _norm(old, eol), _norm(new, eol)
        n = text.count(old)
        if n == 0:
            near = nearest_text(text, old)
            return {"ok": False, "content": "old string not found" + (f" -- {near}" if near else
                                                                      " (nothing in the file comes close: read it)")}
        _ra = a.get("replace_all")
        replace_all = (_ra.strip().lower() in ("true", "1", "yes")
                       if isinstance(_ra, str) else bool(_ra))
        if n > 1 and not replace_all:
            return {"ok": False, "content":
                    f"old string occurs {n} times; add surrounding context to make it "
                    f"unique, or pass replace_all=true to replace every occurrence"}
        after = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        _write_text(p, after, eol)
        self._note_own_write(p)
        self._reindex(os.path.relpath(p, self.root))
        note = f" ({n} occurrences)" if replace_all and n > 1 else ""
        out = {"ok": True, "content": f"edited {rel}{note}" + stale}
        diff = _unified_diff(rel, text, after)
        if diff:
            out["diff"] = diff
        return out

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
                text, eol = _read_text(p)
            except OSError as err:
                problems.append(f"edit {i} ({rel}): {type(err).__name__}: {err}")
                continue
            old, new = _norm(old, eol), _norm(new, eol)
            n = text.count(old)
            if n != 1:
                near = nearest_text(text, old) if n == 0 else ""
                problems.append(f"edit {i} ({rel}): 'old' occurs {n} times (must be exactly 1)"
                                + (f" -- {near}" if near else ""))
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
        # `originals` captures each file's FIRST-SEEN content (before ANY edit
        # in this batch) so the diff shown at the end is one clean before/after
        # per file, not a fragment per edit -- exactly what "what did this
        # multi_edit change" should show.
        applied = []
        originals: dict = {}
        for p, rel, old, new in staged:
            text, eol = _read_text(p)
            if rel not in originals:
                originals[rel] = text
            if text.count(old) != 1:
                return {"ok": False, "content":
                        f"multi_edit stopped at {rel}: an earlier edit in this batch changed "
                        f"the text around 'old' (applied so far: {', '.join(applied) or 'none'}); "
                        f"re-read the file and retry the remaining edits"}
            _write_text(p, text.replace(old, new, 1), eol)
            self._note_own_write(p)
            applied.append(rel)
        for rel in dict.fromkeys(applied):
            self._reindex(os.path.relpath(self.resolve_in_workspace(rel), self.root))
        stale = ("\n⚠ changed on disk since you last read: " + ", ".join(stale_rels)
                 + " — re-read to be safe") if stale_rels else ""
        diffs = []
        for p, rel, _old, _new in staged:
            if rel in originals:
                after, _eol = _read_text(p)
                d = _unified_diff(rel, originals.pop(rel), after)
                if d:
                    diffs.append(d)
        out = {"ok": True,
                "content": f"applied {len(applied)} edits: " + ", ".join(applied) + stale}
        if diffs:
            joined = "\n".join(diffs)
            out["diff"] = joined[:_DIFF_CAP] + (
                f"\n… (diff truncated at {_DIFF_CAP} chars)\n" if len(joined) > _DIFF_CAP else "")
        return out

    def _t_checkpoint(self, a: dict) -> dict:
        if self.shadow is None:
            return {"ok": False, "content": "reversibility is unavailable (no shadow git)"}
        cp = self.shadow.checkpoint(str(a.get("label", "") or "manual"))
        if cp is None:
            return {"ok": False, "content": "checkpoint failed (shadow git error)"}
        note = "created" if cp.get("changed") else "no changes since the last checkpoint"
        # what it holds and what it does not, said (2026-10-30: a checkpoint of a folder of repositories held none of
        # their files and said "created")
        nested = list(cp.get("nested") or [])
        uncovered = list(cp.get("uncovered") or [])
        extra = (f"; holds the nested repositories {', '.join(nested[:8])}" + (" ..." if len(nested) > 8 else "")
                 if nested else "")
        if uncovered:
            extra += "; NOT in it: " + "; ".join(f"{p} ({w})" for p, w in uncovered[:8]) + (
                f" and {len(uncovered) - 8} more" if len(uncovered) > 8 else "")
        return {"ok": True, "content": f"checkpoint cp-{cp.get('n')} ({cp.get('id')}): {note}{extra}",
                **({"uncovered": [list(x) for x in uncovered]} if uncovered else {})}

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

    def _t_shell(self, a: dict) -> dict:
        return self._execute_shell(a)

    def _t_bash(self, a: dict) -> dict:
        # Backward-compatible alias for shell
        return self._execute_shell(a)

    def _execute_shell(self, a: dict) -> dict:
        timeout = min(int(a.get("timeout", 120) or 120), 3600)
        env = os.environ.copy()
        # Fail-soft non-interactive environment: prevent password/sudo/pinentry prompt hangs
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        env.setdefault("SUDO_ASKPASS", "/usr/bin/false")
        env.setdefault("GIT_TERMINAL_PROMPT", "0")

        # Cross-platform PATH & Apple Silicon (M1-M4) vs Intel/Linux/Windows detection
        import sys
        if sys.platform == "darwin":
            # Ensure both Apple Silicon (/opt/homebrew) and Intel (/usr/local) paths exist
            paths = env.get("PATH", "").split(os.pathsep)
            brew_paths = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin"]
            for bp in reversed(brew_paths):
                if os.path.isdir(bp) and bp not in paths:
                    paths.insert(0, bp)
            env["PATH"] = os.pathsep.join(paths)
        elif sys.platform.startswith("linux"):
            paths = env.get("PATH", "").split(os.pathsep)
            linux_paths = ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
            for lp in reversed(linux_paths):
                if os.path.isdir(lp) and lp not in paths:
                    paths.insert(0, lp)
            env["PATH"] = os.pathsep.join(paths)

        # Force UTF-8 environment across all OS/locales
        env.setdefault("LC_ALL", "en_US.UTF-8")
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        # Its own process group (2026-10-30 review: a timeout killed only /bin/sh -- a dev server or pytest workers it
        # started kept running, and the output gathered so far was dropped): on a timeout the whole group stops and
        # what it printed comes back. stderr is merged where it was printed, not appended after stdout.
        posix = os.name != "nt"
        proc = subprocess.Popen(
            a["command"], shell=True, cwd=self.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
            **({"start_new_session": True} if posix else
               {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}))
        timed_out = False
        with self._running_lock:
            self._running.add(proc)
        try:
            raw, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_group(proc, posix)
            try:
                raw, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                raw = b""
        finally:
            with self._running_lock:
                self._running.discard(proc)
        out = _decoded(raw or b"")
        if timed_out:
            out += (f"\n[timed out after {timeout} s -- the command and every process it started were stopped; "
                    "the output above is what it printed until then]")
        full = len(out)
        out = _truncated(out, _BASH_OUT_CAP)
        return {"ok": proc.returncode == 0 and not timed_out,
                "content": out or f"(exit {proc.returncode})",
                "exit_code": proc.returncode,
                **({"timed_out": True} if timed_out else {}),
                "output_chars": full,
                "truncated": full > _BASH_OUT_CAP}

    def _t_grep(self, a: dict) -> dict:
        from webbee.coding_context import WALK_IGNORE_DIRS
        pat = re.compile(a["pattern"])
        base = self.resolve_in_workspace(a.get("path", "."))
        hits = []
        # a single file is searched itself: os.walk() yields nothing for a file, so every grep on
        # one file answered "(no matches)" (live 2026-10-20: 30 of 34 single-file greps were false)
        walk = [(os.path.dirname(base), [], [os.path.basename(base)])] if os.path.isfile(base) else os.walk(base)
        for dp, dns, fns in walk:
            if _is_git_dir(dp):
                dns[:] = []
                continue
            # Prune heavy dependency/build dirs (node_modules, vendor, dist,
            # …) so a grep on a real repo doesn't crawl megabytes of deps.
            dns[:] = [d for d in dns if d not in WALK_IGNORE_DIRS]
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
        shown = hits[:_GREP_HIT_CAP]
        body = "\n".join(shown) or "(no matches)"
        if len(hits) > _GREP_HIT_CAP:
            body += (f"\n\n… ⚠ TRUNCATED: showing {_GREP_HIT_CAP} of {len(hits):,} matches "
                     f"({len(hits) - _GREP_HIT_CAP:,} hidden). Narrow `path` or tighten the "
                     f"pattern -- do NOT assume these are all of them.")
        return {"ok": True, "content": body,
                "match_count": len(hits), "shown": len(shown),
                "truncated": len(hits) > _GREP_HIT_CAP}

    def _t_list_dir(self, a: dict) -> dict:
        """Structured directory listing -- the cheap alternative to `bash ls`.
        Prunes dependency/build dirs (same WALK_IGNORE_DIRS as grep) so a listing
        of a real repo root stays readable, and reports line counts for text
        files so the agent can pick offset/limit for read_file without opening
        the file first."""
        from webbee.coding_context import WALK_IGNORE_DIRS
        base = self.resolve_in_workspace(a.get("path", ".") or ".")
        if not os.path.isdir(base):
            return {"ok": False, "content": f"not a directory: {a.get('path', '.')}"}
        try:
            depth = max(1, min(int(a.get("depth") or 1), 2))
        except (TypeError, ValueError):
            depth = 1

        rows, truncated = [], False
        for dp, dns, fns in os.walk(base):
            if _is_git_dir(dp):
                dns[:] = []
                continue
            dns[:] = sorted(d for d in dns if d not in WALK_IGNORE_DIRS
                            and not d.startswith("."))
            rel_dir = os.path.relpath(dp, base)
            cur = 1 if rel_dir == "." else rel_dir.count(os.sep) + 2
            if cur > depth:
                dns[:] = []
                continue
            for d in dns:
                rows.append(("dir", os.path.relpath(os.path.join(dp, d), base), None, None))
            for fn in sorted(fns):
                if fn.startswith("."):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    continue
                nlines = None
                if size < 2_000_000:
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            nlines = sum(1 for _ in f)
                    except (UnicodeDecodeError, OSError):
                        nlines = None
                rows.append(("file", os.path.relpath(fp, base), size, nlines))
            if cur >= depth:
                dns[:] = []
            if len(rows) > 1000:
                truncated = True
                break

        out = []
        for kind, name, size, nlines in rows[:1000]:
            if kind == "dir":
                out.append(f"  {name}/")
            else:
                meta = f"{size:,}B" + (f", {nlines} lines" if nlines is not None else ", binary")
                out.append(f"  {name}  ({meta})")
        body = "\n".join(out) or "(empty)"
        if truncated:
            body += "\n\n… ⚠ TRUNCATED at 1000 entries — narrow `path` or use depth=1."
        return {"ok": True, "content": body, "entry_count": len(rows),
                "truncated": truncated}

    def _t_glob(self, a: dict) -> dict:
        import glob as _g
        pattern = str(a.get("pattern") or "")
        # inside the workspace only, and bounded (2026-10-30 review: "/etc/*" or "../../**" listed outside it, and a
        # "**" over a big tree came back whole)
        if os.path.isabs(pattern) or ".." in pattern.replace("\\", "/").split("/"):
            return {"ok": False, "content": "glob: the pattern must stay inside the workspace (no absolute path, no ..)"}
        base = os.path.join(self.root, pattern)
        base_real = os.path.realpath(self.root)
        rels = sorted(os.path.relpath(p, self.root) for p in _g.glob(base, recursive=True)
                      if os.path.realpath(p) == base_real or os.path.realpath(p).startswith(base_real + os.sep))
        shown = rels[:_GLOB_MAX]
        more = f"\n… ⚠ {len(rels) - _GLOB_MAX:,} more match(es) not shown -- narrow the pattern" if len(rels) > _GLOB_MAX else ""
        return {"ok": True, "content": ("\n".join(shown) + more) or "(no matches)"}

    def _t_repo_profile(self, a: dict) -> dict:
        return self._cpc("repo_profile", a)

    def _t_graph_slice(self, a: dict) -> dict:
        return self._cpc("graph_slice", a)

    def _t_search_code(self, a: dict) -> dict:
        return self._cpc("search_code", a)

    def _t_impact_of_change(self, a: dict) -> dict:
        return self._cpc("impact_of_change", a)

    def _t_contract_slice(self, a: dict) -> dict:
        return self._cpc("contract_slice", a)

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
        out = self._cpc_local(verb, a)
        # frame phase A: the kernel asks the vault graph first; this local index is the fallback -- links by name
        if isinstance(out, dict) and out.get("ok", True) is not False:
            out = {**out, "coarse": "vault graph unavailable: links by name"}
        return out

    def _t_intel_sync_local(self, a: dict) -> dict:
        """The workspace into the vault's code graph now (frame phase A): the kernel asks before a local code
        question when the vault's copy is behind the last edit. A read: nothing is written or run."""
        if self.indexer is None or not hasattr(self.indexer, "sync_now"):
            return {"ok": False, "content": "intel_sync_local: no code index in this session"}
        out = self.indexer.sync_now()
        if out.get("pending"):     # a state, not a failure: the first sync of the workspace is still running
            return {"ok": True, "content": {"repo_key": "", "pending": out["pending"]}}
        if out.get("ok") is False:
            return {"ok": False, "content": f"intel_sync_local: {out.get('content')}"}
        counts = {k: v for k, v in out.items() if k not in ("repo_key", "synced_at", "repos")}
        # a folder of repositories: each one synced under its own key (``repos``: folder -> key)
        return {"ok": True, "content": {"repo_key": out.get("repo_key") or "", "synced_at": out["synced_at"],
                                        **({"repos": out["repos"]} if out.get("repos") else {}), "counts": counts}}

    def _cpc_local(self, verb: str, a: dict) -> dict:
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
        if verb == "contract_slice":
            return query.contract_slice(self.indexer, a.get("query", ""),
                                        a.get("kind", "all"), int(a.get("k", 50) or 50))
        if verb == "orient":
            return query.orient(self.indexer, a.get("query", ""))
        return {"ok": False, "content": f"unknown cpc verb: {verb}"}

    def _t_intel_sync_remote(self, a: dict) -> dict:
        """Sync a repo on a server the USER reaches with their own ssh into the vault's code graph
        (code graph spec §4.7, plan 3 Task 5): ``{host, root, paths?}``. SFTP only -- no exec channel,
        nothing written on the server, nothing but indexable files read; never prompts: a key that
        needs a passphrase, a hardware touch or 2FA -> ``partial_reason = "needs interactive login"``
        unless an open master connection (``ssh -O check``) carries it."""
        host = str(a.get("host") or "").strip()
        if not host:
            return {"ok": False, "content": "intel_sync_remote: host is required"}
        if self.client_factory is None:
            return {"ok": False, "content": "intel_sync_remote: this session cannot reach the cloud"}
        try:
            out = _run_async(remote_sync(self.client_factory(), host, str(a.get("root") or "."),
                                         [str(p) for p in a.get("paths") or []]))
        except Exception as e:
            return {"ok": False, "content": f"intel_sync_remote: {type(e).__name__}: {e}"}
        return {"ok": bool(out.get("repo_key")), "content": out}

    def _t_web_search(self, a: dict) -> dict:
        """Search the web using Imperal's system web-search extension."""
        query = str(a.get("query", "") or a.get("q", "")).strip()
        if not query:
            return {"ok": False, "content": "'query' argument is missing"}
        if self.client_factory is None:
            return {"ok": False, "content": "web search needs cloud reach, which this session cannot reach right now"}

        num_results = min(max(int(a.get("num_results", 10) or 10), 1), 50)
        params = {"query": query, "num_results": num_results}
        if a.get("include_domains"):
            params["include_domains"] = a["include_domains"]
        if a.get("recency_days"):
            params["recency_days"] = int(a["recency_days"])
        if a.get("type"):
            params["type"] = a["type"]
        if a.get("category"):
            params["category"] = a["category"]

        import asyncio

        async def _call():
            client = self.client_factory()
            return await client.run_tool("web-search", "web_search", params)

        try:
            resp = asyncio.run(_call())
            data = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
            if not resp or (isinstance(resp, dict) and not resp.get("ok", True) and "data" not in resp):
                msg = resp.get("error") or resp.get("message") or "Web search failed"
                return {"ok": False, "content": f"Web search failed: {msg}"}

            items = []
            if isinstance(data, dict):
                items = data.get("items") or data.get("results") or []
            elif isinstance(data, list):
                items = data

            if not items:
                return {"ok": True, "content": f"No web search results found for: {query}"}

            formatted = [f"=== Web search results for '{query}' ==="]
            for i, it in enumerate(items[:num_results], 1):
                t = it.get("title") or "(Untitled)"
                u = it.get("url") or ""
                s = it.get("snippet") or it.get("description") or ""
                formatted.append(f"{i}. {t}\n   URL: {u}\n   Snippet: {s}\n")
            return {"ok": True, "content": "\n".join(formatted).strip()}
        except Exception as e:
            return {"ok": False, "content": f"Web search error: {type(e).__name__}: {e}"}

    def _t_read_url(self, a: dict) -> dict:
        """Read a web page into clean Markdown using Imperal's system web-search extension."""
        url = str(a.get("url", "")).strip()
        if not url:
            return {"ok": False, "content": "'url' argument is missing"}
        if self.client_factory is None:
            return {"ok": False, "content": "reading web pages needs cloud reach, which this session cannot reach right now"}

        params = {"url": url}
        import asyncio

        async def _call():
            client = self.client_factory()
            return await client.run_tool("web-search", "read_url", params)

        try:
            resp = asyncio.run(_call())
            data = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
            if not resp or (isinstance(resp, dict) and not resp.get("ok", True) and "data" not in resp):
                msg = resp.get("error") or resp.get("message") or "Failed to read URL"
                return {"ok": False, "content": f"Read URL failed: {msg}"}

            content = ""
            if isinstance(data, dict):
                content = data.get("content") or data.get("markdown") or data.get("text") or ""
                title = data.get("title") or url
            else:
                content = str(data)
                title = url

            head = f"⟦ {title} · {url} ⟧"
            return {"ok": True, "content": f"{head}\n{content}".strip()}
        except Exception as e:
            return {"ok": False, "content": f"Read URL error: {type(e).__name__}: {e}"}

    def _t_view_image(self, a: dict) -> dict:
        """View a local image file natively with Vision and fallback OCR/text."""
        rel = self._rel(a)
        p = self.resolve_in_workspace(rel)

        if not os.path.exists(p):
            return {"ok": False, "content": f"image file not found: {rel}"}

        from webbee.native_files import classify, human_size
        kind, mime = classify(rel)
        if kind != "image":
            # Best-effort extension guess
            from webbee.native_files import guess_mime
            mime = guess_mime(rel)

        size = os.path.getsize(p)

        import base64
        try:
            with open(p, "rb") as f:
                raw_bytes = f.read()
            b64_img = base64.b64encode(raw_bytes).decode("ascii")
        except Exception as e:
            return {"ok": False, "content": f"failed to read image: {e}"}

        head = f"⟦ {rel} · {human_size(size)} · {mime or 'image'} ⟧"

        fallback_text = ""
        if self.client_factory is not None:
            try:
                from webbee.native_read import read_native
                res = read_native(self.client_factory, p, rel)
                if res.get("ok"):
                    fallback_text = res.get("content", "")
            except Exception:
                pass

        out = {
            "ok": True,
            "native_vision": True,
            "mime_type": mime,
            "image_data": b64_img,
            "size": size,
            "content": f"{head}\n[Native Vision Image Payload: {rel} ({human_size(size)}, {mime})]" + (f"\n\nOCR / Text Context:\n{fallback_text}" if fallback_text else "")
        }
        return out


# ── code graph everywhere (plan 3 Task 5): the user's own SSH feeds the vault's graph ───────────────

def _run_async(coro):
    """The executor runs in a worker thread: a fresh event loop for one call."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def ssh_config_paths() -> list:
    p = os.path.expanduser("~/.ssh/config")
    return [p] if os.path.exists(p) else []


def master_open(host: str) -> bool:
    """An open OpenSSH master connection to ``host`` (``ssh -O check``) -- the user is already logged in."""
    import subprocess
    try:
        r = subprocess.run(["ssh", "-O", "check", host], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def known_fingerprints(host: str) -> list:
    """The host key fingerprints the user's known_hosts holds for the real host an alias resolves to
    (``ssh -G`` for HostName / Port, ``ssh-keygen -lF``) -- local files only."""
    import subprocess
    try:
        g = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=10)
        conf = dict(ln.split(" ", 1) for ln in g.stdout.splitlines() if " " in ln)
        name, port = conf.get("hostname", host), conf.get("port", "22")
        key = name if port == "22" else f"[{name}]:{port}"
        r = subprocess.run(["ssh-keygen", "-lF", key], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({w for ln in r.stdout.splitlines() for w in ln.split() if w.startswith("SHA256:")})


class _Attrs:
    def __init__(self, type_: int, size: int, mtime: float) -> None:
        self.type, self.size, self.mtime = type_, size, mtime


class _Entry:
    def __init__(self, filename: str, attrs: _Attrs) -> None:
        self.filename, self.attrs = filename, attrs


class SystemSftp:
    """The SFTP surface the sdk's walk needs (``readdir`` / ``open``), over the user's system ``sftp``
    in batch mode through an open master connection -- no prompt, no exec channel."""

    _MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                            "Nov", "Dec"), start=1)}

    def __init__(self, host: str) -> None:
        self.host = host
        self._dirs: dict = {}        # dir -> its entries (lstat answers from the parent's listing)
        self._real: dict = {}        # dir -> the server's resolved path of it

    async def _batch(self, commands: str) -> str:
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "sftp", "-q", "-b", "-", "-o", "BatchMode=yes", self.host, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(commands.encode("utf-8"))
        if proc.returncode != 0:
            raise OSError(err.decode("utf-8", "replace").strip()[:200] or "sftp failed")
        return out.decode("utf-8", "replace")

    @staticmethod
    def _quote(path: str) -> str:
        # one batch line per command: a control character in a path (a newline names a second command,
        # and sftp's "!" runs a LOCAL shell) is refused -- names come from the server's listing
        p = str(path)
        if any(ord(c) < 32 or ord(c) == 127 for c in p):
            raise OSError("refused: a control character in a remote path")
        return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _mtime(self, mon: str, day: str, when: str) -> float:
        import datetime
        try:
            if ":" in when:
                year = datetime.date.today().year
                hh, mm = when.split(":")
                dt = datetime.datetime(year, self._MONTHS[mon], int(day), int(hh), int(mm))
            else:
                dt = datetime.datetime(int(when), self._MONTHS[mon], int(day))
            return dt.timestamp()
        except (KeyError, ValueError):
            return 0.0

    async def readdir(self, path: str) -> list:
        out = await self._batch(f"ls -la {self._quote(path)}\n")
        entries = []
        for ln in out.splitlines():
            parts = ln.split(None, 8)
            if len(parts) < 9 or parts[0][:1] not in "-dl":
                continue
            name = parts[8].split(" -> ", 1)[0] if parts[0][0] == "l" else parts[8]
            name = name.rsplit("/", 1)[-1]
            kind = {"d": 2, "l": 3}.get(parts[0][0], 1)
            entries.append(_Entry(name, _Attrs(kind, int(parts[4]) if parts[4].isdigit() else 0,
                                               self._mtime(parts[5], parts[6], parts[7]))))
        return entries

    async def lstat(self, path: str):
        """The entry's own attributes (a symlink is type 3), from its directory's listing."""
        import posixpath
        d, b = posixpath.split(str(path).rstrip("/") or "/")
        if d not in self._dirs:
            self._dirs[d] = {e.filename: e.attrs for e in await self.readdir(d or "/")}
        if b not in self._dirs[d]:
            raise OSError(path)
        return self._dirs[d][b]

    async def realpath(self, path: str) -> str:
        """Where the server resolves ``path`` (``cd`` + ``pwd``: the links on the way resolved) -- the sdk
        keeps every read under the repo root by it. A file resolves through its directory."""
        import posixpath
        p = str(path).rstrip("/") or "/"
        if p not in self._real:
            try:
                out = await self._batch(f"cd {self._quote(p)}\npwd\n")
                line = next((ln for ln in out.splitlines() if ln.startswith("Remote working directory:")), "")
                if not line:
                    raise OSError(path)
                self._real[p] = line.split(":", 1)[1].strip()
            except OSError:
                d, b = posixpath.split(p)
                if not b or d == p:
                    raise
                self._real[p] = posixpath.join(await self.realpath(d or "/"), b)
        return self._real[p]

    def open(self, path: str, mode: str = "rb"):
        sftp = self

        class _Reader:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def read(self_inner, n: int = -1) -> bytes:
                import tempfile
                with tempfile.TemporaryDirectory() as d:
                    local = os.path.join(d, "f")
                    await sftp._batch(f"get {sftp._quote(path)} {sftp._quote(local)}\n")
                    with open(local, "rb") as f:
                        return f.read() if n is None or n < 0 else f.read(n)
        return _Reader()


async def _open_sftp(host: str):
    """``(sftp, conn, fingerprints, why)``: asyncssh over the user's ssh config, agent and known_hosts
    (never a prompt), else the system sftp over an open master connection, else ``why``."""
    if master_open(host):
        return SystemSftp(host), None, known_fingerprints(host), ""
    try:
        import asyncssh
    except ImportError:
        return None, None, [], "asyncssh is not installed"
    try:
        conn = await asyncssh.connect(host, config=ssh_config_paths(), passphrase=None, connect_timeout=20)
    except (asyncssh.PermissionDenied, asyncssh.KeyImportError, asyncssh.KeyEncryptionError):
        return None, None, [], "needs interactive login"
    except Exception as e:      # unreachable, refused, a host key that changed: said, never guessed past
        return None, None, [], f"{type(e).__name__}: {e}"
    key = conn.get_server_host_key()
    # the negotiated key and the host's other keys the user's known_hosts trusts: a server has several (RSA,
    # ECDSA, ed25519) and another source may have met it by a different one -- one machine, never two
    fps = sorted({key.get_fingerprint("sha256")} if key else set())
    fps = sorted(set(fps) | set(known_fingerprints(host)))
    return await conn.start_sftp_client(), conn, fps, ""


async def _no_repo_at(sftp, ci, root: str) -> str:
    """Why ``root`` on the server is no repository to sync ("" when it is one): it must exist and be (inside) a
    repository -- a ``.git`` or a manifest at it or above."""
    try:
        await sftp.readdir(root)
    except Exception as e:
        return f"{root} does not exist or is not a readable directory ({type(e).__name__})"
    repo_root = await ci.sftp_repo_root(sftp, root)
    try:
        names = {e.filename for e in await sftp.readdir(repo_root)}
    except Exception:
        names = set()
    if not names & (set(getattr(ci, "_REPO_MARKERS", (".git",))) | set(getattr(ci, "_MANIFESTS", ()))):
        return f"{root} is in no repository (no .git or project manifest at it or above)"
    return ""


async def remote_head(host: str, root: str) -> dict:
    """The commit the server's repo at ``root`` is on -- a few small SFTP reads (``.git/HEAD`` and its ref), never a
    listing: ``{"commit", "branch", "root", "why"}``; ``why`` says what failed (an unknown host, a missing folder)."""
    from imperal_sdk import code_index as ci
    sftp, conn, _fps, why = await _open_sftp(host)
    if sftp is None:
        return {"commit": "", "branch": "", "root": root, "why": f"{host}: {why}"}
    try:
        missing = await _no_repo_at(sftp, ci, root)
        if missing:
            return {"commit": "", "branch": "", "root": root, "why": f"{host}: {missing}"}
        repo_root = await ci.sftp_repo_root(sftp, root)
        ident = ci.git_identity((await ci.sftp_read_meta(sftp, repo_root))["files"])
        return {"commit": ident.get("commit") or "", "branch": ident.get("branch") or "", "root": repo_root, "why": ""}
    except Exception as e:
        return {"commit": "", "branch": "", "root": root, "why": f"{host}: {type(e).__name__}: {e}"}
    finally:
        if conn is not None:
            conn.close()


async def remote_sync(client, host: str, root: str, paths: list) -> dict:
    """List and read the repo holding ``root`` on ``host`` over SFTP (the sdk's one walk: .gitignore,
    secrets and dependency trees skipped), extract and embed here, and sync it into the vault through
    the gateway (the sdk's one sync). ``paths``: only these files changed (a partial listing)."""
    import posixpath
    import time

    from imperal_sdk import code_index as ci
    from webbee.intel.service import vault_sync
    t0 = time.monotonic()
    sftp, conn, fps, why = await _open_sftp(host)
    if sftp is None:
        return {"repo_key": "", "partial_reason": why, "content": f"{host}: {why}"}
    try:
        missing = await _no_repo_at(sftp, ci, root)
        if missing:
            # never a walk of the folder above (live 2026-10-25: a root that did not exist synced /home for 30 min)
            return {"repo_key": "", "partial_reason": missing, "content": f"{host}: {missing}"}
        repo_root = await ci.sftp_repo_root(sftp, root)
        try:     # the repo's identity is its real path: a symlinked dir names the release it points into
            real_root = str(await sftp.realpath(repo_root) or repo_root) if hasattr(sftp, "realpath") else repo_root
        except Exception:
            real_root = repo_root
        meta_files = (await ci.sftp_read_meta(sftp, repo_root))["files"]
        if paths:
            listing = []
            for p in paths:
                rel = p[len(repo_root) + 1:] if p.startswith(repo_root.rstrip("/") + "/") else p
                try:
                    st = await sftp.stat(posixpath.join(repo_root, rel)) if hasattr(sftp, "stat") else None
                    listing.append({"path": rel, "size": int(getattr(st, "size", 0) or 0),
                                    "mtime": float(getattr(st, "mtime", 0) or 0)} if st else {"path": rel, "size": 0,
                                                                                             "mtime": time.time()})
                except Exception:
                    listing.append({"path": rel, "size": -1, "mtime": 0.0})
            tree = {"items": listing, "partial": "", "os": ci.sftp_os(repo_root)}
        else:
            tree = await ci.sftp_list_tree(sftp, repo_root)
        meta = {"machine": {"fingerprints": fps,
                            "machine_id": meta_files.get("/etc/machine-id", b"").decode("utf-8", "replace").strip(),
                            "label": host},
                "root": real_root, "root_path": real_root, "os": tree["os"], "source": "system_ssh", "ref": host,
                "machine_kind": "ssh", "git": ci.git_identity(meta_files), "listing_complete": not paths}
        if tree.get("partial"):
            meta["partial_listing"] = tree["partial"]

        async def read(ps):
            got, todo = {}, list(ps)
            for _ in range(64):
                if not todo:
                    break
                ans = await ci.sftp_read_many(sftp, repo_root, todo)
                got.update(ans["files"])
                todo = list(ans["rest"])
            return got
        out = await vault_sync(client, meta, tree["items"], read)
    finally:
        if conn is not None:
            conn.close()
    log_line = (f"code sync repo={out.get('repo_key')} source=system_ssh listed={out.get('listed')} "
                f"changed={out.get('changed')} read_bytes={out.get('read_bytes')} new_blobs={out.get('new_blobs')} "
                f"extract_ms={out.get('extract_ms')} links+={out.get('links_added')} links-={out.get('links_removed')} "
                f"unresolved={out.get('unresolved')} partial={out.get('partial_reason') or '-'} "
                f"ms={int((time.monotonic() - t0) * 1000)}")
    import logging
    logging.getLogger("webbee.intel").info(log_line)
    return {**out, "root": repo_root, "host": host}
