from __future__ import annotations

import asyncio
from typing import Callable, Optional, Protocol

from .git import (
    get_clone_path,
    gh_list_prs,
    git_update_pristine,
    remove_clone,
)
from .processor import PRProcessor
from .state import PRDisplayInfo, PRState, StateManager
from .display import build_display_list


class PollHost(Protocol):
    _active_tasks: dict[tuple[str, int], asyncio.Task]

    def on_log(self, text: str, level: str) -> None: ...
    def on_status_update(self, repo: str, pr_number: int, status: str, error: Optional[str]) -> None: ...
    def on_pr_list(self, prs: list[PRDisplayInfo]) -> None: ...


async def poll_loop(
    host: PollHost,
    state_manager: StateManager,
    poll_interval_minutes: int,
    recent_minutes: int,
) -> None:
    while True:
        try:
            repos = await state_manager.get_repos()
            if not repos:
                host.on_log("No repos configured.", "warn")
            else:
                host.on_log(f"Polling {len(repos)} repo(s)…", "info")
                new_tasks: list[asyncio.Task] = []

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

                    # Remove state + clones for PRs no longer in the list.
                    current_numbers = {str(p["number"]) for p in prs}
                    for old_num, _ in (await state_manager.get_all_pr_states(repo)).items():
                        if old_num not in current_numbers:
                            remove_clone(get_clone_path(repo, int(old_num)))
                            await state_manager.remove_pr(repo, old_num)
                            host.on_log(f"Removed PR #{old_num} ({repo}) from state", "info")

                    # Adopt local branches that now have PRs.
                    pr_branches = {p["headRefName"] for p in prs}
                    for branch in await state_manager.get_local_branches(repo):
                        if branch in pr_branches:
                            await state_manager.remove_local_branch(repo, branch)
                            host.on_log(f"Branch {branch} ({repo}) now has a PR — adopted", "info")

                    # Ensure all known PRs have stub state so they appear immediately.
                    for pr_data in prs:
                        pn = str(pr_data["number"])
                        if await state_manager.get_pr_state(repo, pn) is None:
                            await state_manager.upsert_pr_state(repo, pn, PRState(
                                title=pr_data.get("title", ""),
                                branch=pr_data["headRefName"],
                                created_at=pr_data.get("createdAt", ""),
                            ))

                    _repos = await state_manager.get_repos()
                    host.on_pr_list(await build_display_list(_repos, state_manager))

                    for pr_data in prs:
                        pr_number: int = pr_data["number"]
                        key = (repo, pr_number)
                        existing = host._active_tasks.get(key)
                        if existing and not existing.done():
                            continue

                        def make_status_cb() -> Callable[[str, int, str, Optional[str]], None]:
                            def cb(rr: str, nn: int, status: str, err: Optional[str]) -> None:
                                host.on_status_update(rr, nn, status, err)
                            return cb

                        def make_log_cb() -> Callable[[str, str], None]:
                            def cb(text: str, level: str) -> None:
                                host.on_log(text, level)
                            return cb

                        processor = PRProcessor(
                            repo=repo,
                            pr_data=pr_data,
                            state_manager=state_manager,
                            status_cb=make_status_cb(),
                            log_cb=make_log_cb(),
                        )
                        task = asyncio.create_task(processor.process(recent_minutes))
                        host._active_tasks[key] = task
                        new_tasks.append(task)

                if new_tasks:
                    await asyncio.gather(*new_tasks, return_exceptions=True)

            repos = await state_manager.get_repos()
            display = await build_display_list(repos, state_manager)
            host.on_pr_list(display)

        except asyncio.CancelledError:
            return
        except Exception as e:
            host.on_log(f"Poll loop error: {e}", "error")

        # Poll more frequently when any PR is waiting for CI checks.
        sleep_minutes = poll_interval_minutes
        try:
            any_pending = False
            for repo in await state_manager.get_repos():
                for _, pr_state in (await state_manager.get_all_pr_states(repo)).items():
                    if pr_state.status == "pending":
                        any_pending = True
                        break
                if any_pending:
                    break
            if any_pending:
                sleep_minutes = 1
        except Exception:
            pass
        await asyncio.sleep(sleep_minutes * 60)
