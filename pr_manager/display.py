from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .state import PRDisplayInfo, StateManager


def format_age(created_at_str: Optional[str]) -> str:
    if not created_at_str:
        return ""
    try:
        created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        days = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h"
        minutes = delta.seconds // 60
        return f"{minutes}m"
    except Exception:
        return "?"


def format_review_status(pr_state) -> str:
    if pr_state.is_draft:
        return "draft"
    decision = pr_state.review_decision
    if decision == "APPROVED":
        return "approved"
    if decision == "CHANGES_REQUESTED":
        return "changes requested"
    if decision == "REVIEW_REQUIRED":
        return "review needed"
    if pr_state.review_count > 0:
        return "in review"
    return ""


def format_activity(pr_state) -> str:
    parts = []
    total = pr_state.comment_count
    if total > 0:
        parts.append(f"{total} comment{'s' if total != 1 else ''}")
    if pr_state.latest_activity:
        age = format_age(pr_state.latest_activity)
        if age:
            parts.append(f"latest {age} ago")
    return ", ".join(parts)


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
                is_active=False,
                error_message=pr_state.error_message,
                review_status=format_review_status(pr_state),
                activity=format_activity(pr_state),
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
