import argparse
import asyncio
import os

from webbee import __version__
from webbee.config import Config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webbee", description="Webbee 🐝 — coding agent in your terminal")
    p.add_argument("--version", action="version", version=f"webbee {__version__}")
    p.add_argument("--mode", choices=["default", "plan", "autopilot"], default="default")
    p.add_argument(
        "--marathon", metavar="GOAL", default=None,
        help="Launch a long-horizon marathon toward GOAL (runs autonomously "
             "until the goal is met, verified by the project's own test command).",
    )
    p.add_argument(
        "--once", "--no-marathon", dest="once", action="store_true",
        help="Run a single bounded coding turn (stops at the step limit) instead "
             "of the default self-driving marathon.",
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("login", help="Log in to your Imperal account in the browser")
    sub.add_parser("logout", help="Log out and remove local credentials")
    return p


def _quiet_downloads() -> None:
    """Silence dependency download-progress bars + telemetry BEFORE anything
    imports them (model2vec / huggingface_hub / tokenizers). A stray tqdm or HF
    progress bar written to stderr corrupts the full-screen prompt_toolkit dock
    — it shows up as overlapping/duplicated text (the dock owns the terminal and
    diffs the screen; any external write desyncs the diff). `setdefault` so an
    explicit user override still wins."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def main(argv=None) -> None:
    _quiet_downloads()
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()

    if args.cmd == "login":
        from imperal_mcp import auth
        try:
            email = asyncio.run(auth.login_device(cfg))
            print(f"Logged in as {email}.")
        except KeyboardInterrupt:
            print("\nLogin cancelled.")
        except Exception as e:
            print(f"Login failed: {e}")
        return
    if args.cmd == "logout":
        from imperal_mcp import auth
        asyncio.run(auth.logout(cfg))
        print("Logged out.")
        return

    if args.marathon:
        # --marathon GOAL launches a single autonomous marathon and streams it,
        # then exits. Reuses the same AgentSession + stream reader as coding.
        from webbee.repl import run_marathon
        try:
            _print_boot_progress("checking for updates")
            _maybe_print_update_notice()
            _print_boot_progress("booting workspace (indexing, git, session)")
            asyncio.run(run_marathon(cfg, args.mode, args.marathon))
        except KeyboardInterrupt:
            print("\nBye 🐝")
        return

    # Default: the polished REPL. Fire a non-blocking update-check first.
    from webbee.repl import run_repl
    try:
        # webbee-code-boot-visibility-v1 (Valentin, live, Linux boxes: "просто
        # висит в терминале \"webbee\" и хуй поймешь, работает или комп
        # завис"): everything between process start and the first dock frame
        # -- the update-notice PyPI fetch (network, up to ~2s timeout) and
        # boot_workspace's intel/shadow/git jobs (now parallel, but a large
        # repo's indexing can still take real seconds) -- used to run with
        # ZERO output, so a slow network or a big repo looked indistinguishable
        # from a hang. These are cheap, ONE-LINE, self-overwriting progress
        # notes printed to the real (non-full-screen-yet) terminal -- gone the
        # instant the dock takes over the screen, never left behind as clutter.
        _print_boot_progress("checking for updates")
        _maybe_print_update_notice()
        _print_boot_progress("booting workspace (indexing, git, session)")
        asyncio.run(run_repl(cfg, args.mode, once=args.once))
    except KeyboardInterrupt:
        # Ctrl-C during the update-check fetch, or at the read_line() prompt,
        # unwinds here — exit clean, no traceback. (repl.py itself now cancels
        # a Ctrl-C mid-turn internally and returns to the prompt instead of
        # propagating — see run_repl.)
        print("\nBye 🐝")


def _print_boot_progress(what: str) -> None:
    """One-line, best-effort boot progress note (webbee-code-boot-
    visibility-v1) -- printed to the plain terminal BEFORE the full-screen
    dock takes over, so a slow network check or a big-repo index doesn't
    look like a frozen process. `\r` + trailing spaces overwrite the
    previous note in place rather than stacking lines; the dock's own first
    frame paints over this the instant it starts, so nothing lingers.
    Skips entirely on a non-tty (piped/redirected output, CI) where an
    overwriting \r note would just print as garbage."""
    try:
        import sys
        if not sys.stdout.isatty():
            return
        sys.stdout.write(f"\r🐝 {what}...".ljust(60))
        sys.stdout.flush()
    except Exception:
        pass  # a progress note must never block or crash startup


def _maybe_print_update_notice() -> None:
    try:
        from pathlib import Path
        import time
        from webbee.update import check_for_update, default_fetch
        cache = Path(os.path.expanduser("~/.cache/webbee/update.json"))
        notice = check_for_update(__version__, cache_path=cache, now=time.time(), fetch=default_fetch)
        if notice:
            print(notice)
    except Exception:
        pass  # update-check must never block or crash startup
