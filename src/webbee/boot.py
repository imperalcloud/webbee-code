"""REPL boot helpers (extracted verbatim from webbee.repl): everything the
dock sets up before the first prompt — the cached git branch, the dock's
stderr log file, the intel/shadow default factories and their guarded
starters, and the best-effort boot replay of the durable coding thread.
All fail-soft: boot must never crash over a nice-to-have."""
import asyncio
import os
import subprocess


def _git_branch(workspace: str) -> str:
    try:
        p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace,
                           capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else "-"
    except (OSError, subprocess.SubprocessError):
        return "-"


def _open_dock_stderr_log():
    """A file to swallow stderr for the full-screen dock's lifetime. The dock is
    a prompt_toolkit full-screen Application that OWNS the terminal (it diffs the
    screen); ANY stray write to stderr while it runs — a dependency's tqdm
    download bar, a library warning, a background watcher-task traceback —
    desyncs that diff and shows up as overlapping/duplicated text. Routing
    stderr to ~/.cache/webbee/tui-stderr.log keeps the dock pixel-clean while
    still preserving errors for debugging. Falls back to os.devnull if the cache
    dir is unwritable; NEVER raises."""
    try:
        d = os.path.expanduser("~/.cache/webbee")
        os.makedirs(d, exist_ok=True)
        return open(os.path.join(d, "tui-stderr.log"), "a", buffering=1, encoding="utf-8")
    except Exception:
        try:
            return open(os.devnull, "w")
        except Exception:
            import io
            return io.StringIO()


def _default_shadow_factory(cfg, workspace: str):
    """The reversibility shadow git. Guarded like intel: any failure (no git
    binary, cache not writable) degrades to None -- coding still works, just
    without the time machine."""
    from webbee.checkpoints import ShadowGit, shadow_key
    from webbee.repo import find_repo_root
    root = find_repo_root(workspace)
    sg = ShadowGit(root, shadow_key(root), cache_dir=cfg.cache_dir)
    return sg if sg.ensure() else None


def _default_intel_factory(cfg, workspace: str):
    """Lazy/guarded -- a base install (no tree-sitter/watchfiles extra) must
    never fail to import here; the intel boot wraps the whole build in
    try/except so any error (missing extra, indexing failure) degrades to
    `intel=None` rather than crashing the REPL."""
    from webbee.intel.service import IntelService
    from webbee.repo import compute_repo_key, find_repo_root
    root = find_repo_root(workspace)
    return IntelService(root, compute_repo_key(root), cache_dir=cfg.cache_dir)


async def replay_thread(cfg, token_provider, sink) -> None:
    """Boot replay of the durable per-user thread (Task 9): best-effort,
    entirely swallowed on any failure -- history is a nice-to-have,
    never a boot blocker (network down, no such session yet, etc.)."""
    try:
        from imperal_mcp.client import ImperalClient
        from webbee.thread import (conversational_text, fetch_recent_thread,
                                   truncate_for_display)
        _iid = await ImperalClient(cfg, token_provider).whoami()
        _msgs = await fetch_recent_thread(cfg, token_provider, f"marathon-{_iid}-rboot")
        _shown = 0
        for _m in _msgs[-40:]:
            _text = conversational_text(_m.get("content", ""))
            if not _text:
                continue  # pure tool traffic -- mind-food, not conversation
            sink.foreign_turn(_m.get("surface", "terminal"), _m.get("role", ""),
                              truncate_for_display(_text))
            _shown += 1
        if _shown:
            sink.note("— live —")
    except Exception:
        pass  # replay is best-effort; never block boot


async def start_intel(cfg, workspace: str, intel_factory):
    """Off-loop intel build (indexing does sync file I/O + subprocess). Any
    failure here (missing extra, bad repo, etc.) must degrade to
    (None, None), never crash the boot -- coding still works, just
    without repo intelligence. Returns (intel, watcher_task)."""
    try:
        svc = (intel_factory or _default_intel_factory)(cfg, workspace)
        await asyncio.to_thread(svc.build)
        from webbee.intel import watch
        watcher_task = asyncio.ensure_future(watch.watch_workspace(svc.root, svc.apply_changes))
        return svc, watcher_task
    except Exception:
        return None, None


async def start_shadow(cfg, workspace: str, shadow_factory):
    """Whole-mind P4: the reversibility shadow (never the user's VCS);
    guarded -- boot must not fail over the time machine."""
    try:
        return await asyncio.to_thread(shadow_factory or _default_shadow_factory, cfg, workspace)
    except Exception:
        return None
