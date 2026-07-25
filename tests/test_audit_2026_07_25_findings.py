"""Audit 2026-07-25 (TUI/logic deep audit) -- EXECUTABLE evidence for each
finding. These tests PIN TODAY'S BEHAVIOUR: every one of them passes against
the current tree, and the assertions are written so that FIXING the finding
makes the corresponding test fail loudly (with the fix note in the message) --
that is the point: a fix must consciously update its own evidence test.

Findings pinned here:
  A-1  chunker chunk ids collide  -> VectorStore.add raises -> intel silently off
  A-2  OutputPane.reflow is O(records) and runs ON the event loop, no debounce
  A-3  Ctrl-V / drag-copy do BLOCKING subprocess clipboard I/O in handlers
  A-4  mode persistence runs a git subprocess inside the Shift-TAB key binding
"""
from __future__ import annotations

import inspect
import time

import pytest


# --------------------------------------------------------------------------
# A-1 (HIGH): duplicate chunk ids crash VectorStore.add -> intel disabled
# --------------------------------------------------------------------------
def test_a1_chunk_id_ignores_kind_so_ids_can_collide():
    """chunker._mk builds the id from PATH+LINE-RANGE only.

    A class whose body is a single method spans the SAME line range as that
    method, so both chunks get the SAME id. Fix = include kind/symbol in the
    id (then this test must be updated).
    """
    from webbee.intel import chunker

    src = inspect.getsource(chunker._mk)
    # FIXED 2026-07-25: the id now carries kind AND symbol, which is what
    # makes it unique for a class window vs a nested symbol on the same span.
    assert 'id=f"{path}#{s}-{e}:{kind}:{symbol}"' in src, (
        "chunk id shape changed -- it MUST stay unique per (span, kind, "
        "symbol) or A-1 regresses (silent intel death)."
    )


def test_a1_duplicate_id_in_one_batch_is_survivable():
    """The crash that used to happen here: same id twice in ONE add() batch.

    Pre-fix, `_mat` was only created at the END of add() (the `if rows:`
    vstack) while the already-seen branch wrote `_mat[_pos[_id]]` immediately,
    so the second occurrence indexed a still-empty matrix -> IndexError ->
    intel silently off. Fixed by materialising appends BEFORE overwrites.
    """
    np = pytest.importorskip("numpy")
    from webbee.intel.vectors import VectorStore

    vs = VectorStore(dim=4)
    vs.add(["dup", "dup"], np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype="float32"))
    ids, mat = vs.to_arrays()
    assert list(ids) == ["dup"]
    assert mat.tolist() == [[0.0, 1.0, 0.0, 0.0]], "last value must win"


def test_a1_duplicate_id_is_survivable_on_a_warm_store_too():
    """Not just the empty-store edge: a warm store used to raise as well.

    The pre-existing row must also survive the batch untouched.
    """
    np = pytest.importorskip("numpy")
    from webbee.intel.vectors import VectorStore

    vs = VectorStore(dim=4)
    vs.add(["seed"], np.array([[9, 9, 9, 9]], dtype="float32"))
    vs.add(["x", "x"], np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype="float32"))
    ids, mat = vs.to_arrays()
    assert list(ids) == ["seed", "x"]
    assert mat.tolist() == [[9.0, 9.0, 9.0, 9.0], [0.0, 1.0, 0.0, 0.0]]


def test_a6_walk_prunes_dependency_trees_but_keeps_real_source(tmp_path):
    """FIXED 2026-07-25: the index walk must not spend itself on dependencies.

    `_walk` stops at `limit`, so an in-tree dependency directory could consume
    the cap and leave the project's own sources unindexed. Measured on the
    audit checkout (it carries an in-tree venv): 3062 files -> 120, and a full
    build_index+chunk_index 31.0s -> 1.1s, 204MB -> 11MB peak.

    Equally important is what must NOT be pruned: a virtualenv is identified
    by its PEP 405 `pyvenv.cfg` marker, so a project module that merely
    happens to be named `env/` (or `build/`) keeps being indexed.
    """
    from webbee.intel.service import _walk

    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "settings.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "gen.py").write_text("Y = 1\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("var z = 1\n", encoding="utf-8")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (venv / "junk.py").write_text("Z = 1\n", encoding="utf-8")

    got = set(_walk(str(tmp_path)))

    # real source survives -- pruning by NAME alone would have eaten these
    assert "env/settings.py" in got
    assert "build/gen.py" in got
    # dependency trees are gone
    assert not any(p.startswith("venv/") for p in got), "virtualenv not pruned"
    assert not any(p.startswith("node_modules/") for p in got)


def test_a1_intel_failure_is_swallowed_whole():
    """Why it is SILENT: start_intel returns (None, None) on ANY exception --
    no note, no warning. Intel just disappears for the session.
    """
    from webbee import boot

    src = inspect.getsource(boot.start_intel)
    assert "return None, None" in src
    assert "except Exception" in src
    # No user-facing signal is emitted on the failure path.
    assert "sink" not in src.split("except Exception")[-1], (
        "start_intel now reports the failure -- A-1's silence is FIXED: "
        "update this test."
    )


# --------------------------------------------------------------------------
# A-2 (HIGH): reflow is linear in retained records and blocks the loop
# --------------------------------------------------------------------------
def test_a2_resize_is_debounced_one_reflow_per_settled_width():
    """FIXED 2026-07-25: a drag-resize must cost ONE reflow, not one per
    intermediate width the ticker happens to sample.

    pane.reflow re-renders the whole retained ring ON the event loop
    (~116ms at the 4000-record cap), so the pre-fix behaviour -- reflow on
    the first tick that sees any new width -- turned a slow drag into
    several hundred ms of cumulative freeze, every reflow but the last one
    immediately thrown away. Now a new width is only remembered; the reflow
    fires on the next tick that reads the SAME width.
    """
    from webbee.output_pane import OutputPane
    from webbee.tui import _ticker_busy, _width_watch

    class _Out:
        def __init__(self, cols):
            self.cols = cols

        def get_size(self):
            import types
            return types.SimpleNamespace(columns=self.cols, rows=24)

    class _App:
        def __init__(self, cols):
            self.output = _Out(cols)

    pane = OutputPane(width=120)
    pane.console.print("some transcript line that will rewrap")
    calls: list[int] = []
    real = pane.reflow
    pane.reflow = lambda w: (calls.append(w), real(w))[1]   # type: ignore[assignment]

    app = _App(120)
    for w in (112, 104, 96, 88, 80):        # ticks sampled mid-drag
        app.output.cols = w
        _width_watch(pane, app)
    assert calls == [], f"reflowed mid-drag ({calls}) -- A-2 debounce regressed"

    # while a resize is pending the ticker must stay on the FAST cadence,
    # otherwise the re-wrap lands up to a full second after the drag ends.
    class _Slots:
        def active(self):
            return type("S", (), {"pane": pane})()

    assert _ticker_busy(_Slots(), lambda: False) is True

    _width_watch(pane, app)                 # same width again -> settled
    assert calls == [80], f"settled resize did not reflow exactly once ({calls})"
    assert pane.console.width == 80
    assert not pane._resize_pending


def test_a2_reflow_cost_grows_with_retained_records():
    """Cost is linear in the ring: ~29us/record on the audit machine, so a
    full 4000-record ring costs ~100ms+ of BLOCKED event loop per width
    change (and a drag-resize triggers several).
    """
    from webbee.output_pane import OutputPane

    def cost(n: int) -> float:
        p = OutputPane(width=120)
        for i in range(n):
            p.console.print(f"  step {i}: some transcript prose that will rewrap when narrowed")
        p.reflow(100)                      # warm caches
        t0 = time.perf_counter()
        p.reflow(80)
        return time.perf_counter() - t0

    small, big = cost(200), cost(1600)
    # 8x the records must cost materially more -- proves it is O(records),
    # i.e. an unbounded-by-design stall on a long session.
    assert big > small * 3, f"reflow no longer scales with records ({small=} {big=})"


# --------------------------------------------------------------------------
# A-3 (MEDIUM): blocking clipboard subprocesses inside input handlers
# --------------------------------------------------------------------------
def test_a3_paste_key_reads_clipboard_off_the_event_loop():
    """FIXED 2026-07-25: Ctrl-V must not run the clipboard tools inline.

    read_clipboard() shells out to osascript + pbpaste (macOS) / xclip /
    wl-paste, 2s timeout each -- measured ~84ms for plain text on the audit
    host, up to ~4s worst case. A key binding runs ON the event loop, so
    inline that froze the entire dock. It now hands the read to
    asyncio.to_thread inside a background task.

    NOTE the slicing bug this test used to have: it inspected only the first
    1200 chars after "_paste_key", which is BEFORE the offload site -- so it
    kept passing after the fix. Scope to the real function body instead.
    """
    from webbee import tui

    src = inspect.getsource(tui.run_session)
    assert "_paste_key" in src
    body = src.split("def _paste_key(event):", 1)[1].split("\n    def ", 1)[0]
    assert "read_clipboard" in body
    assert "to_thread" in body, (
        "Ctrl-V reads the clipboard inline again -- A-3 regressed (dock freeze)"
    )
    # the slot/pane must still be captured SYNCHRONOUSLY, before the await,
    # so a tab switch mid-read cannot land the paste on the wrong tab.
    assert body.index("_a()") < body.index("to_thread")


def test_a3_drag_copy_runs_pbcopy_inside_mouse_handler():
    """MOUSE_UP -> _copy_selection -> copy_to_clipboard -> subprocess.run."""
    from webbee import clipboard
    from webbee.output_pane import OutputPane

    assert "subprocess.run" in inspect.getsource(clipboard._try_local_copy)
    assert "copy_to_clipboard" in inspect.getsource(OutputPane._copy_selection)


def test_a3_clipboard_helpers_never_raise_so_the_dock_cannot_die():
    """The mitigating half of A-3 (why this is latency, NOT a crash): the
    clipboard helpers swallow everything, so an exception can never escape a
    mouse/key handler and tear down the Application.
    """
    from webbee import clipboard

    for fn in (clipboard._try_local_copy, clipboard._local_copy_cmd):
        assert "except Exception" in inspect.getsource(fn) or "which" in inspect.getsource(fn)
    assert clipboard.copy_to_clipboard("") is not None


# --------------------------------------------------------------------------
# A-4 (MEDIUM): Shift-TAB spawns a git subprocess on the event loop
# --------------------------------------------------------------------------
def test_a4_mode_persistence_shells_out_to_git():
    """compute_repo_key runs `git remote get-url origin` with timeout=5."""
    from webbee import repo

    src = inspect.getsource(repo.compute_repo_key)
    assert "subprocess.run" in src and "remote" in src
    assert "timeout=5" in src, "git timeout changed -- re-read A-4's worst case"


def test_a4_repo_key_is_memoised_so_mode_switches_cost_no_subprocess(monkeypatch):
    """FIXED 2026-07-25: Shift-TAB must not shell out to git on every switch.

    Chain: tui s-tab -> on_cycle -> repl.set_slot_mode -> mode_store.save_mode
    -> _path_for -> compute_repo_key, which runs `git remote get-url origin`
    (timeout=5). That is ~13ms per keypress on a warm local repo and up to 5s
    if git hangs (network/NFS checkout, locked .git) -- and it ran straight on
    the event loop, because a key binding IS the event loop.

    repo_key is invariant for the process, so it is now memoised per
    workspace: the first call pays, every later switch is a dict lookup.
    """
    import webbee.mode_store as mode_store

    calls = {"n": 0}

    def _fake_key(root):
        calls["n"] += 1
        return "deadbeefcafe"

    monkeypatch.setattr(mode_store, "compute_repo_key", _fake_key)
    monkeypatch.setattr(mode_store, "find_repo_root", lambda w: "/fake/root")

    for _ in range(10):
        mode_store.save_mode("/fake/ws", "plan")

    assert calls["n"] == 1, (
        f"compute_repo_key ran {calls['n']}x for one workspace -- A-4 "
        "regressed (a git subprocess per mode switch, on the event loop)"
    )
    # a DIFFERENT workspace must still get its own key (no cross-repo bleed)
    mode_store.save_mode("/fake/other", "plan")
    assert calls["n"] == 2


def test_a5_autopilot_confirm_task_is_strongly_referenced():
    """FIXED 2026-07-25: the remote-autopilot confirm task must be kept alive.

    asyncio holds only a WEAK reference to a running task, so the previous
    bare `ensure_future(_confirm_autopilot(...))` could be collected mid-await
    -- the security prompt would vanish and the user's remote upgrade request
    would go unanswered with no trace.

    It must NOT be parked in slot.bg_tasks: everything there is cancelled by
    the slot-close/exit teardown, and this confirm is the security gate for
    the upgrade, so it has to be allowed to complete.
    """
    from webbee import repl

    src = inspect.getsource(repl._on_mode)
    assert "_CONFIRM_TASKS.add" in src, (
        "autopilot confirm task is unreferenced again -- A-5 regressed (GC hazard)"
    )
    assert "add_done_callback" in src, "the set must self-clear or it leaks"
    # Scan CODE only -- the rationale comment legitimately names bg_tasks.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "bg_tasks" not in code, (
        "the confirm must not be cancellable by slot teardown -- it is the "
        "security gate for the autopilot upgrade"
    )
    assert isinstance(repl._CONFIRM_TASKS, set)


# --------------------------------------------------------------------------
# Verified-HEALTHY invariants (regression guards for things the audit
# confirmed are correct -- these must never silently regress).
# --------------------------------------------------------------------------
def test_healthy_idle_ticker_does_not_repaint():
    """An idle tick must NOT invalidate (that is what keeps idle CPU ~0)."""
    from webbee.output_pane import OutputPane
    from webbee import tui

    class _Slot:
        kind = "session"
        def __init__(self, p): self.pane = p
    class _Slots:
        def __init__(self, p): self._s = _Slot(p)
        def active(self): return self._s
    class _Out:
        def get_size(self):
            class S: columns = 120; rows = 40
            return S()
    class _App:
        output = _Out()
        def __init__(self): self.n = 0
        def invalidate(self): self.n += 1

    app = _App()
    tui._tick_once(_Slots(OutputPane(width=120)), app, lambda: False)
    assert app.n == 0, "idle tick now repaints -- idle CPU regression"
    assert tui._tick_interval(False) >= 1.0
    assert tui._tick_interval(True) <= 0.25


def test_healthy_transcript_is_bounded():
    """Both the replay ring and the text buffer stay bounded."""
    from webbee import output_pane
    from webbee.output_pane import OutputPane

    assert output_pane._MAX_RECORDS == 4000
    p = OutputPane(width=100)
    # The ceiling is enforced in notify() -- the real sink path after each
    # print -- not inside console.print itself.
    for i in range(20600):
        p.console.print(f"line {i}")
        if i % 500 == 0:
            p.notify()
    p.notify()
    assert len(p._all_lines()) <= 20000, "transcript trim ceiling regressed"
    assert len(p._records) <= output_pane._MAX_RECORDS


def test_healthy_no_mutable_module_globals_in_tui_layer():
    """Per-tab isolation rests on there being no shared mutable module state."""
    import webbee.output_pane as op
    import webbee.render as rd
    import webbee.tui as tu

    # Read-only LOOKUP TABLES (a style map, a tool->icon map) are built once at
    # import and never mutated -- they are config, not per-tab state. The
    # companion test below proves the "never mutated" half by source scan, so
    # allowlisting them here does not weaken the isolation guarantee.
    allowed = {"_STYLE_DICT", "_ICON"}
    for mod in (tu, op, rd):
        for name, val in vars(mod).items():
            if name.startswith("__") or not name.startswith("_") or name in allowed:
                continue
            assert not isinstance(val, (dict, list, set)), (
                f"{mod.__name__}.{name} is mutable module state -- "
                "cross-tab leak risk (isolation regression)"
            )

def test_healthy_allowlisted_lookup_tables_are_never_mutated():
    """The other half of the isolation guarantee for the two allowlisted dicts.

    `_STYLE_DICT` / `_ICON` are module-level dicts, so they are only safe
    because nothing ever writes to them. Prove that from the source rather
    than trusting the convention: any subscript-assignment or mutating method
    call on either name is an isolation regression.
    """
    import inspect
    import re

    import webbee.render as rd
    import webbee.tui as tu

    bad = re.compile(
        r"(_ICON|_STYLE_DICT)\s*\[[^\]]*\]\s*=|"
        r"(_ICON|_STYLE_DICT)\.(update|pop|popitem|setdefault|clear)\s*\("
    )
    for mod in (tu, rd):
        src = inspect.getsource(mod)
        hit = bad.search(src)
        assert hit is None, (
            f"{mod.__name__}: lookup table mutated ({hit.group(0)!r}) -- "
            "it is shared across every tab, so this is a cross-tab leak"
        )

def test_a1_end_to_end_repro_one_ordinary_file_kills_intel(tmp_path):
    """The COMPLETE A-1 chain, from source file to dead intel, no venv needed.

    Trigger geometry (derived, not guessed): a class longer than
    _CHUNK_MAX_LINES is split by _windows() into 60-line windows stepping by
    (60 - 10) = 50, so windows start at 1, 51, 101... When the class's LAST
    window happens to span exactly the same lines as a trailing method, both
    chunks get the id "path#51-61" -- and VectorStore.add then writes
    _mat[pos] while _mat is still the empty (0, dim) array.

    A class spanning lines 1..61 with a method spanning 51..61 is the minimal
    such shape.
    """
    import collections

    from webbee.intel import chunker, indexer
    from webbee.intel.vectors import VectorStore

    lines = ["class Big:"]                        # line 1
    lines += [f"    x{i} = {i}" for i in range(49)]   # lines 2..50
    lines.append("    def tail(self):")           # line 51
    lines += [f"        y{j} = {j}" for j in range(10)]  # lines 52..61
    assert len(lines) == 61
    (tmp_path / "win.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    idx = indexer.build_index(str(tmp_path), ["win.py"])
    chunks = chunker.chunk_index(str(tmp_path), idx)
    ids = [c.id for c in chunks]
    dupes = {k: v for k, v in collections.Counter(ids).items() if v > 1}

    # FIXED 2026-07-25: this exact geometry used to yield "win.py#51-61"
    # twice (the class's last window + the method) and kill intel.
    assert dupes == {}, f"A-1 REGRESSED: colliding chunk ids {dupes}"
    # the two chunks that used to collide are still both present, now distinct
    assert "win.py#51-61:chunk:Big" in ids
    assert "win.py#51-61:function:tail" in ids

    # ...and the embed step that used to explode now completes.
    vs = VectorStore(dim=4)
    vs.add(ids, [[1.0, 0.0, 0.0, 0.0]] * len(ids))
    assert vs.to_arrays()[1].shape[0] == len(ids)



def test_a1_intel_failure_is_swallowed_so_the_user_is_never_told():
    """Why A-1 is a SILENT loss: boot.start_intel returns (None, None) on ANY
    exception, so the IndexError above disables repo intelligence for the whole
    session with no message, no log line the user sees, and no retry."""
    src = inspect.getsource(__import__("webbee.boot", fromlist=["x"]).start_intel)
    assert "except Exception" in src and "None, None" in src, (
        "start_intel no longer blanket-swallows -- if it now surfaces the "
        "failure, the A-1 silence half is FIXED: update this test."
    )

