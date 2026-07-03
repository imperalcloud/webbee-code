import argparse
import asyncio
import os

from webbee import __version__
from webbee.config import Config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webbee", description="Webbee 🐝 — coding agent in your terminal")
    p.add_argument("--version", action="version", version=f"webbee {__version__}")
    p.add_argument("--mode", choices=["default", "plan", "autopilot"], default="default")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("login", help="Log in to your Imperal account in the browser")
    sub.add_parser("logout", help="Log out and remove local credentials")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()

    if args.cmd == "login":
        from imperal_mcp import auth
        print(f"Logged in as {asyncio.run(auth.login_device(cfg))}.")
        return
    if args.cmd == "logout":
        from imperal_mcp import auth
        asyncio.run(auth.logout(cfg))
        print("Logged out.")
        return

    # Default: the polished REPL. Fire a non-blocking update-check first.
    from webbee.repl import run_repl
    try:
        _maybe_print_update_notice()
        asyncio.run(run_repl(cfg, args.mode))
    except KeyboardInterrupt:
        # Ctrl-C during the update-check fetch, or at the read_line() prompt,
        # unwinds here — exit clean, no traceback. (repl.py itself now cancels
        # a Ctrl-C mid-turn internally and returns to the prompt instead of
        # propagating — see run_repl.)
        print("\nBye 🐝")


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
