from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .state import PRDisplayInfo, StateManager


def format_age(created_at_str: Optional[str]) -> str:
    if not created_at_str:
        return "?"
    try:
        created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        days = delta.days
        hours = delta.seconds // 3600
        return f"{days}d {hours}h" if days else f"{hours}h"
    except Exception:
        return "?"


async def build_display_list(
    repos: list[str], state_manager: StateManager
) -> list[PRDisplayInfo]:
    result: list[PRDisplayInfo] = []
    for repo in repos:
        for num_str, pr_state in (await state_manager.get_all_pr_states(repo)).items():
            try:
                number = int(num_str)
            except ValueError:
                continue
            result.append(PRDisplayInfo(
                repo=repo,
                number=number,
                title=pr_state.title or f"PR #{number}",
                branch=pr_state.branch or "",
                status=pr_state.status,
                age=format_age(pr_state.created_at),
                is_active=pr_state.status in ("rebasing", "fixing_ci"),
                error_message=pr_state.error_message,
            ))
        # Include local branches (no PR yet).
        for branch in await state_manager.get_local_branches(repo):
            result.append(PRDisplayInfo(
                repo=repo,
                number=0,
                title=f"(local) {branch}",
                branch=branch,
                status="local",
                age="",
                is_active=False,
                error_message=None,
            ))
    result.sort(key=lambda p: (p.repo, -p.number))
    return result
