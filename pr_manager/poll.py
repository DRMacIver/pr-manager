from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Protocol

from .display import build_display_list
from .git import (
    gh_list_prs,
    gh_pr_check_status,
    get_clone_path,
    git_commits_behind,
    git_setup_pr_clone,
    git_update_pristine,
    remove_clone,
)
from .state import PRDisplayInfo, PRState, StateManager


class PollHost(Protocol):
    _active_tasks: dict[tuple[str, int], asyncio.Task]

    def on_log(self, text: str, level: str) -> None: ...
    def on_status_update(self, repo: str, pr_number: int, status: str, error: Optional[str]) -> None: ...
    def on_pr_list(self, prs: list[PRDisplayInfo]) -> None: ...


async def compute_pr_status(repo: str, pr_data: dict, clone_path: Path) -> str:
    """Read-only status derivation for a single PR.

    Never writes to GitHub or to the working tree beyond the `git fetch`
    embedded in `git_commits_behind`.
    """
    branch = pr_data["headRefName"]
    base = pr_data.get("baseRefName") or "main"
    behind = await git_commits_behind(clone_path, branch, base)
    if behind > 0:
        return "behind"
    check_status, _details = await gh_pr_check_status(repo, int(pr_data["number"]))
    # gh_pr_check_status returns: green | pending | failing | no_checks
    return check_status


async def poll_loop(
    host: PollHost,
    state_manager: StateManager,
    poll_interval_minutes: int,
    recent_minutes: int,
    nudge: Optional[asyncio.Event] = None,
) -> None:
    """Status-only poll loop. Never writes to PRs.

    `recent_minutes` is accepted for signature compatibility with the
    previous auto-fix loop; it is unused.
    """
    del recent_minutes  # retained in signature for API stability
    while True:
        try:
            repos = await state_manager.get_repos()
            if not repos:
                host.on_log("No repos configured.", "warn")
            else:
                host.on_log(f"Polling {len(repos)} repo(s)…", "info")
                for repo in repos:
                    try:
                        prs = await gh_list_prs(repo)
                    except Exception as e:
                        host.on_log(f"Failed to list PRs for {repo}: {e}", "error")
                        continue

                    try:
                        await git_update_pristine(repo)
                    except Exception as e:
                        host.on_log(f"Failed to fetch {repo}: {e}", "error")
                        continue

                    current_numbers = {str(p["number"]) for p in prs}

                    # Remove state + clones for PRs no longer in the list.
                    for old_num, _ in (await state_manager.get_all_pr_states(repo)).items():
                        if old_num not in current_numbers:
                            try:
                                deleted = remove_clone(get_clone_path(repo, int(old_num)))
                            except Exception as e:
                                host.on_log(f"Failed to clean up PR #{old_num} ({repo}): {e}", "warn")
                                continue
                            if deleted:
                                await state_manager.remove_pr(repo, old_num)
                                host.on_log(f"Removed PR #{old_num} ({repo}) from state", "info")
                            else:
                                host.on_log(
                                    f"PR #{old_num} ({repo}) gone from gh pr list but clone is recent — keeping state",
                                    "warn",
                                )

                    # Adopt local branches that now have PRs.
                    pr_branches = {p["headRefName"] for p in prs}
                    for branch in await state_manager.get_local_branches(repo):
                        if branch in pr_branches:
                            await state_manager.remove_local_branch(repo, branch)
                            host.on_log(f"Branch {branch} ({repo}) now has a PR — adopted", "info")

                    # Ensure stub state for new PRs.
                    for pr_data in prs:
                        pn = str(pr_data["number"])
                        existing = await state_manager.get_pr_state(repo, pn)
                        if existing is None:
                            existing = PRState(
                                title=pr_data.get("title", ""),
                                branch=pr_data["headRefName"],
                                created_at=pr_data.get("createdAt", ""),
                            )
                            await state_manager.upsert_pr_state(repo, pn, existing)

                    # Per-PR status refresh.
                    for pr_data in prs:
                        pn = str(pr_data["number"])
                        try:
                            await git_setup_pr_clone(repo, int(pn), pr_data["headRefName"])
                            clone = get_clone_path(repo, int(pn))
                            status = await compute_pr_status(repo, pr_data, clone)
                        except Exception as e:
                            host.on_log(f"Status check failed for #{pn} ({repo}): {e}", "warn")
                            status = "error"

                        st = await state_manager.get_pr_state(repo, pn) or PRState(
                            title=pr_data.get("title", ""),
                            branch=pr_data["headRefName"],
                            created_at=pr_data.get("createdAt", ""),
                        )
                        st.title = pr_data.get("title", st.title)
                        st.branch = pr_data["headRefName"]
                        st.created_at = pr_data.get("createdAt", st.created_at)
                        st.is_draft = pr_data.get("isDraft", False)
                        st.review_decision = pr_data.get("reviewDecision", "") or ""
                        comments = pr_data.get("comments", []) or []
                        reviews = pr_data.get("reviews", []) or []
                        st.comment_count = len(comments) + len(reviews)
                        st.review_count = len(reviews)
                        timestamps = (
                            [c.get("createdAt", "") for c in comments]
                            + [r.get("submittedAt", "") for r in reviews]
                        )
                        st.latest_activity = max(timestamps) if timestamps else None
                        st.status = status
                        st.error_message = None
                        await state_manager.upsert_pr_state(repo, pn, st)
                        host.on_status_update(repo, int(pn), status, None)

                    host.on_pr_list(await build_display_list(await state_manager.get_repos(), state_manager))

            host.on_pr_list(await build_display_list(await state_manager.get_repos(), state_manager))

        except asyncio.CancelledError:
            return
        except Exception as e:
            host.on_log(f"Poll loop error: {e}", "error")

        sleep_minutes = poll_interval_minutes
        if nudge is not None:
            nudge.clear()
            try:
                await asyncio.wait_for(nudge.wait(), timeout=sleep_minutes * 60)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(sleep_minutes * 60)
