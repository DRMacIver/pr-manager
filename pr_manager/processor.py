from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .agent import AgentRunner
from .git import (
    get_clone_path,
    get_log_path,
    gh_get_recent_commits,
    gh_pr_check_status,
    git_commits_behind_main,
    git_get_current_sha,
    git_get_new_commits_since,
    git_latest_commit_is_bot,
    git_push_force_with_lease,
    git_reattribute_and_push,
    git_setup_pr_clone,
)
from .state import PRState, StateManager


class PRProcessor:
    def __init__(
        self,
        repo: str,
        pr_data: dict,
        state_manager: StateManager,
        status_cb: Callable[[str, int, str, Optional[str]], None],
        log_cb: Callable[[str, str], None],
    ) -> None:
        self._repo = repo
        self._pr_data = pr_data
        self._state_manager = state_manager
        self._status_cb = status_cb
        self._log_cb = log_cb

    async def process(self, recent_minutes: int) -> None:
        pr_number: int = self._pr_data["number"]
        branch: str = self._pr_data["headRefName"]
        title: str = self._pr_data.get("title", "")
        created_at: str = self._pr_data.get("createdAt", "")

        try:
            pr_state = await self._state_manager.get_pr_state(self._repo, str(pr_number)) or PRState()
            pr_state.title = title
            pr_state.branch = branch
            pr_state.created_at = created_at
            pr_state.last_checked = datetime.now(timezone.utc).isoformat()
            pr_state.is_draft = self._pr_data.get("isDraft", False)
            pr_state.review_decision = self._pr_data.get("reviewDecision", "")
            comments = self._pr_data.get("comments", [])
            reviews = self._pr_data.get("reviews", [])
            pr_state.comment_count = len(comments) + len(reviews)
            pr_state.review_count = len(reviews)
            # Latest activity: most recent comment or review timestamp.
            timestamps = [c.get("createdAt", "") for c in comments] + [r.get("submittedAt", "") for r in reviews]
            pr_state.latest_activity = max(timestamps) if timestamps else None
            await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)

            clone_path = get_clone_path(self._repo, pr_number)
            log_path = get_log_path(self._repo, pr_number)

            self._log_cb(f"Checking PR #{pr_number} ({self._repo}/{branch})", "info")
            await git_setup_pr_clone(self._repo, pr_number, branch)

            pr_state = await self._state_manager.get_pr_state(self._repo, str(pr_number)) or PRState(
                title=title, branch=branch, created_at=created_at,
            )

            # 1. Skip if there are recent human commits.
            if await self._has_human_changes(branch, pr_state, recent_minutes):
                pr_state.status = "human_changes"
                pr_state.error_message = "Recent human commits — skipping"
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
                self._status_cb(self._repo, pr_number, "human_changes", None)
                return

            # 2. Rebase if behind main.
            behind = await git_commits_behind_main(clone_path, branch)
            if behind > 0:
                self._log_cb(
                    f"PR #{pr_number} is {behind} commit(s) behind main — rebasing", "info"
                )
                await self._do_rebase(pr_number, branch, clone_path, pr_state, log_path)
                return

            # 3. Check CI status.
            check_status, failures = await gh_pr_check_status(self._repo, pr_number)

            # 3a. No checks at all — likely a bot push that can't trigger Actions.
            if check_status == "no_checks":
                if await git_latest_commit_is_bot(self._repo, branch):
                    self._log_cb(
                        f"PR #{pr_number} has no checks (bot push) — reattributing commit", "info"
                    )
                    pushed = await git_reattribute_and_push(clone_path, branch)
                    if pushed:
                        old_sha = await git_get_current_sha(clone_path)
                        await self._state_manager.record_our_commits(
                            self._repo, str(pr_number), [old_sha]
                        )
                        pr_state.status = "pending"
                        pr_state.error_message = None
                        await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
                        self._status_cb(self._repo, pr_number, "pending", None)
                        self._log_cb(f"PR #{pr_number} reattributed and pushed ✓ — waiting for checks", "info")
                    else:
                        self._set_error(pr_state, pr_number, "Failed to reattribute bot commit")
                    return
                # No checks and not a bot — treat as pending (checks may not exist yet).
                pr_state.status = "pending"
                pr_state.error_message = None
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
                self._status_cb(self._repo, pr_number, "pending", None)
                self._log_cb(f"PR #{pr_number} ({self._repo}) no checks reported yet", "info")
                return

            if check_status == "failing":
                self._log_cb(f"PR #{pr_number} has failing checks — fixing CI", "info")
                await self._do_ci_fix(pr_number, branch, clone_path, pr_state, log_path, failures)
                return
            if check_status == "pending":
                pr_state.status = "pending"
                pr_state.error_message = None
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
                self._status_cb(self._repo, pr_number, "pending", None)
                self._log_cb(f"PR #{pr_number} ({self._repo}) checks still running", "info")
                return

            # 4. All good.
            pr_state.status = "green"
            pr_state.error_message = None
            await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
            self._status_cb(self._repo, pr_number, "green", None)
            self._log_cb(f"PR #{pr_number} ({self._repo}) ✓ green", "info")

        except asyncio.CancelledError:
            try:
                s = await self._state_manager.get_pr_state(self._repo, str(pr_number)) or PRState()
                s.status = "pending"
                s.error_message = None
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), s)
                self._status_cb(self._repo, pr_number, "pending", None)
            except Exception:
                pass
            raise
        except Exception as e:
            self._log_cb(f"Error processing PR #{pr_number} ({self._repo}): {e}", "error")
            self._status_cb(self._repo, pr_number, "error", str(e))
            try:
                s = await self._state_manager.get_pr_state(self._repo, str(pr_number)) or PRState()
                s.status = "error"
                s.error_message = str(e)
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), s)
            except Exception:
                pass

    async def _has_human_changes(
        self, branch: str, pr_state: PRState, recent_minutes: int
    ) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=recent_minutes)
        recent_shas = await gh_get_recent_commits(
            self._repo, branch, cutoff.isoformat()
        )
        our_set = set(pr_state.our_commits)
        return any(sha not in our_set for sha in recent_shas)

    async def _do_rebase(
        self,
        pr_number: int,
        branch: str,
        clone_path: Path,
        pr_state: PRState,
        log_path: Path,
    ) -> None:
        pr_state.status = "rebasing"
        pr_state.error_message = None
        await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
        self._status_cb(self._repo, pr_number, "rebasing", None)

        old_sha = await git_get_current_sha(clone_path)
        runner = AgentRunner(
            self._repo, pr_number, branch, clone_path, self._state_manager, log_path
        )
        result = await runner.run_rebase()

        if result and "DONE" in result.upper():
            pushed = await git_push_force_with_lease(clone_path, branch)
            if pushed:
                new_commits = await git_get_new_commits_since(clone_path, old_sha)
                await self._state_manager.record_our_commits(
                    self._repo, str(pr_number), new_commits
                )
                pr_state = (
                    await self._state_manager.get_pr_state(self._repo, str(pr_number))
                    or pr_state
                )
                pr_state.status = "pending"
                pr_state.error_message = None
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
                self._status_cb(self._repo, pr_number, "pending", None)
                self._log_cb(f"PR #{pr_number} rebased and pushed ✓ — waiting for checks", "info")
            else:
                self._set_error(pr_state, pr_number, "Push rejected (force-with-lease failed)")
        else:
            self._set_error(pr_state, pr_number, "Rebase agent did not complete (check log with [v])")

    async def _do_ci_fix(
        self,
        pr_number: int,
        branch: str,
        clone_path: Path,
        pr_state: PRState,
        log_path: Path,
        failures: str,
    ) -> None:
        pr_state.status = "fixing_ci"
        pr_state.error_message = None
        await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
        self._status_cb(self._repo, pr_number, "fixing_ci", None)

        old_sha = await git_get_current_sha(clone_path)
        runner = AgentRunner(
            self._repo, pr_number, branch, clone_path, self._state_manager, log_path
        )
        result = await runner.run_ci_fix(failures)
        result_upper = (result or "").upper()

        if "UNFIXABLE" in result_upper:
            self._set_error(
                pr_state, pr_number,
                "CI failures appear unrelated to PR changes (check log with [v])",
            )
        elif "DONE" in result_upper:
            new_sha = await git_get_current_sha(clone_path)
            if new_sha != old_sha:
                pushed = await git_push_force_with_lease(clone_path, branch)
                if not pushed:
                    self._set_error(pr_state, pr_number, "Push rejected after CI fix")
                    return
                new_commits = await git_get_new_commits_since(clone_path, old_sha)
                await self._state_manager.record_our_commits(
                    self._repo, str(pr_number), new_commits
                )
                self._log_cb(f"PR #{pr_number} CI fix pushed ✓", "info")
            else:
                self._log_cb(f"PR #{pr_number} CI fix: agent made no commits", "info")

            pr_state = (
                await self._state_manager.get_pr_state(self._repo, str(pr_number))
                or pr_state
            )
            pr_state.status = "pending"
            pr_state.error_message = None
            await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
            self._status_cb(self._repo, pr_number, "pending", None)
        else:
            self._set_error(pr_state, pr_number, "CI fix agent did not complete (check log with [v])")

    def _set_error(self, pr_state: PRState, pr_number: int, msg: str) -> None:
        pr_state.status = "error"
        pr_state.error_message = msg
        asyncio.create_task(
            self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
        )
        self._status_cb(self._repo, pr_number, "error", msg)
        self._log_cb(f"PR #{pr_number} error: {msg}", "error")
