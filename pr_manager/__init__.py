from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback

from .state import StateManager


def main() -> None:
    try:
        _main()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        print("\nPress q to exit...", flush=True)
        while True:
            c = sys.stdin.read(1)
            if c in ("q", "Q", ""):
                break
        sys.exit(1)


def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="pr-manager",
        description="GitHub PR auto-manager with Claude agent integration",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Start the TUI manager")
    run_p.add_argument(
        "--recent-minutes", type=int, default=30, metavar="N",
        help="Ignore PRs with human commits within the last N minutes (default: 30)",
    )
    run_p.add_argument(
        "--poll-interval", type=int, default=5, metavar="N",
        help="Polling interval in minutes (default: 5)",
    )
    run_p.add_argument(
        "--headless", action="store_true",
        help="Run without TUI — just log to stdout (useful for debugging)",
    )

    add_p = sub.add_parser("add", help="Add a repo to manage")
    add_p.add_argument("repo", help="owner/repo")

    rem_p = sub.add_parser("remove", help="Stop managing a repo")
    rem_p.add_argument("repo", help="owner/repo")

    sub.add_parser("list", help="List all managed repos")

    args = parser.parse_args()
    state_manager = StateManager()

    if args.command == "run":
        if args.headless:
            from .headless import run_headless
            asyncio.run(state_manager.load())
            asyncio.run(run_headless(state_manager, args.poll_interval, args.recent_minutes))
        else:
            if not os.environ.get("TMUX"):
                script_dir = os.path.dirname(os.path.abspath(__file__))
                os.execvp("tmux", [
                    "tmux", "new-session", "-s", "pr-manager", "--",
                    "uv", "run", "--project", script_dir + "/..",
                    "pr-manager", "run",
                    "--poll-interval", str(args.poll_interval),
                    "--recent-minutes", str(args.recent_minutes),
                ])
            from .tui import PRManagerApp
            asyncio.run(state_manager.load())
            PRManagerApp(state_manager, args.poll_interval, args.recent_minutes).run()

    elif args.command == "add":
        async def _add() -> None:
            await state_manager.load()
            await state_manager.add_repo(args.repo)
            print(f"Added {args.repo}")
        asyncio.run(_add())

    elif args.command == "remove":
        async def _remove() -> None:
            await state_manager.load()
            await state_manager.remove_repo(args.repo)
            print(f"Removed {args.repo}")
        asyncio.run(_remove())

    elif args.command == "list":
        async def _list() -> None:
            await state_manager.load()
            repos = await state_manager.get_repos()
            if repos:
                for r in repos:
                    print(r)
            else:
                print("No repos configured. Use: pr-manager add owner/repo")
        asyncio.run(_list())


if __name__ == "__main__":
    main()
