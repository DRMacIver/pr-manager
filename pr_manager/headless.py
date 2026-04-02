from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from .constants import STATUS_STYLE
from .poll import poll_loop
from .state import PRDisplayInfo, StateManager


class HeadlessRunner:
    """Minimal non-TUI runner that logs everything to stdout."""

    def __init__(self) -> None:
        self._active_tasks: dict[tuple[str, int], asyncio.Task] = {}

    def on_log(self, text: str, level: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {"error": "ERR", "warn": "WRN"}.get(level, "   ")
        print(f"[{ts}] {prefix} {text}", flush=True)

    def on_status_update(self, repo: str, pr_number: int, status: str, error: Optional[str]) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        icon, _ = STATUS_STYLE.get(status, ("?", ""))
        msg = f"[{ts}]  {icon}  PR #{pr_number} ({repo}) -> {status}"
        if error:
            msg += f": {error}"
        print(msg, flush=True)

    def on_pr_list(self, prs: list[PRDisplayInfo]) -> None:
        if not prs:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] --- PR summary ---", flush=True)
        for pr in prs:
            icon, _ = STATUS_STYLE.get(pr.status, ("?", ""))
            review = f" [{pr.review_status}]" if pr.review_status else ""
            activity = f" ({pr.activity})" if pr.activity else ""
            line = f"  {icon} #{pr.number:>4}  {pr.repo:<30} {pr.branch:<35} {pr.status}{review}{activity}"
            if pr.error_message:
                line += f"  ERR: {pr.error_message}"
            print(line, flush=True)


async def run_headless(
    state_manager: StateManager,
    poll_interval: int,
    recent_minutes: int,
) -> None:
    host = HeadlessRunner()
    await poll_loop(host, state_manager, poll_interval, recent_minutes)
