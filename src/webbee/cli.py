import argparse
import asyncio
import os

from webbee.config import Config
from webbee.session import AgentSession


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="webbee")
    parser.add_argument("--mode", choices=["default", "plan", "autopilot"], default="default")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login", help="Log in to your Imperal account in the browser")
    sub.add_parser("logout", help="Log out and remove local credentials")
    args = parser.parse_args(argv)

    cfg = Config.from_env()

    if args.cmd == "login":
        from imperal_mcp import auth

        email = auth.login(cfg)
        print(f"Logged in as {email}.")
        return

    if args.cmd == "logout":
        from imperal_mcp import auth

        asyncio.run(auth.logout(cfg))
        print("Logged out.")
        return

    # default: REPL — read a task line from stdin, run a coding session, print the answer.
    from imperal_mcp import auth

    async def token_provider() -> str:
        return await auth.ensure_access_token(cfg)

    session = AgentSession(cfg, token_provider, os.getcwd(), args.mode)

    while True:
        try:
            task = input("webbee> ")
        except EOFError:
            return
        if not task.strip():
            continue
        text = asyncio.run(session.run(task))
        print(text)
