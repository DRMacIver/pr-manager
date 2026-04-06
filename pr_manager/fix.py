from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .agent import AgentRunner
from .git import (
    get_clone_path,
    get_log_path,
    gh_pr_check_status,
    git_commits_behind,
    git_get_current_sha,
    git_get_new_commits_since,
    git_latest_commit_is_bot,
    git_push_force_with_lease,
    git_reattribute_and_push,
    git_setup_pr_clone,
    git_update_pristine,
    run_cmd,
)
from .state import PRState, StateManager


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(text: str, level: str = "info") -> None:
    prefix = {"error": "ERR", "warn": "WRN"}.get(level, "   ")
    print(f"[{_ts()}] {prefix} {text}", flush=True)


def parse_pr_url(url: str) -> tuple[str, int]:
    """Parse a GitHub PR URL into (owner/repo, pr_number)."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"Invalid PR URL: {url}")
    return m.group(1), int(m.group(2))


async def _fetch_pr_data(repo: str, pr_number: int) -> dict:
    """Fetch PR metadata via gh CLI."""
    _, out, _ = await run_cmd([
        "gh", "pr", "view", str(pr_number), "--repo", repo,
        "--json", "number,title,headRefName,baseRefName,body,isDraft,state",
    ])
    return json.loads(out)


async def run_fix(url: str, poll_interval: int = 60) -> None:
    """Fix a single PR: rebase on target and loop until CI is green."""
    repo, pr_number = parse_pr_url(url)
    _log(f"Fixing PR #{pr_number} in {repo}")

    pr_data = await _fetch_pr_data(repo, pr_number)
    if pr_data.get("state") not in ("OPEN", "open"):
        _log(f"PR is {pr_data.get('state', 'unknown')} — nothing to do", "error")
        sys.exit(1)

    branch = pr_data["headRefName"]
    target_branch = pr_data["baseRefName"]
    _log(f"Branch: {branch}, target: {target_branch}")

    _log("Setting up pristine clone...")
    await git_update_pristine(repo)
    _log("Setting up working clone...")
    await git_setup_pr_clone(repo, pr_number, branch)

    clone_path = get_clone_path(repo, pr_number)
    log_path = get_log_path(repo, pr_number)

    state_manager = StateManager()
    await state_manager.load()

    pr_state = await state_manager.get_pr_state(repo, str(pr_number)) or PRState(
        title=pr_data.get("title", ""),
        branch=branch,
    )
    await state_manager.upsert_pr_state(repo, str(pr_number), pr_state)

    while True:
        # 1. Rebase if behind target.
        behind = await git_commits_behind(clone_path, branch, target_branch)
        if behind > 0:
            _log(f"{behind} commit(s) behind {target_branch} — rebasing")
            success = await _do_rebase(
                repo, pr_number, branch, clone_path, log_path,
                target_branch, state_manager,
            )
            if not success:
                _log("Rebase failed — exiting", "error")
                sys.exit(1)
            _log("Rebased and pushed — waiting for CI")
            await asyncio.sleep(poll_interval)
            continue

        # 2. Check CI status.
        check_status, failures = await gh_pr_check_status(repo, pr_number)

        if check_status == "green":
            _log("CI is green — done!")
            return

        if check_status == "pending":
            _log("CI checks still running — waiting")
            await asyncio.sleep(poll_interval)
            continue

        if check_status == "no_checks":
            if await git_latest_commit_is_bot(repo, branch):
                _log("No checks (bot push) — reattributing commit")
                pushed = await git_reattribute_and_push(clone_path, branch)
                if pushed:
                    sha = await git_get_current_sha(clone_path)
                    await state_manager.record_our_commits(
                        repo, str(pr_number), [sha],
                    )
                    _log("Reattributed and pushed — waiting for checks")
                else:
                    _log("Failed to reattribute bot commit", "error")
                    sys.exit(1)
            else:
                _log("No checks reported yet — waiting")
            await asyncio.sleep(poll_interval)
            continue

        # 3. CI is failing — fix it.
        _log("CI failing — attempting fix")
        success = await _do_ci_fix(
            repo, pr_number, branch, clone_path, log_path,
            failures, state_manager, pr_data.get("title", ""),
        )
        if not success:
            _log("CI fix failed — exiting", "error")
            sys.exit(1)
        _log("CI fix complete — waiting for checks")
        await asyncio.sleep(poll_interval)


async def _do_rebase(
    repo: str,
    pr_number: int,
    branch: str,
    clone_path: Path,
    log_path: Path,
    target_branch: str,
    state_manager: StateManager,
) -> bool:
    old_sha = await git_get_current_sha(clone_path)
    runner = AgentRunner(
        repo, pr_number, branch, clone_path, state_manager, log_path,
        log_to_stdout=True,
    )
    result = await runner.run_rebase(target_branch)

    if result and "DONE" in result.upper():
        pushed = await git_push_force_with_lease(clone_path, branch)
        if pushed:
            new_commits = await git_get_new_commits_since(clone_path, old_sha)
            await state_manager.record_our_commits(
                repo, str(pr_number), new_commits,
            )
            return True
        _log("Push rejected (force-with-lease failed)", "error")
        return False
    _log("Rebase agent did not complete", "error")
    return False


async def _do_ci_fix(
    repo: str,
    pr_number: int,
    branch: str,
    clone_path: Path,
    log_path: Path,
    failures: str,
    state_manager: StateManager,
    pr_title: str,
) -> bool:
    old_sha = await git_get_current_sha(clone_path)
    runner = AgentRunner(
        repo, pr_number, branch, clone_path, state_manager, log_path,
        log_to_stdout=True,
    )
    result = await runner.run_ci_fix(failures)
    result_upper = (result or "").upper()

    if "UNFIXABLE" in result_upper:
        _log("Agent claims UNFIXABLE — reviewing")
        review_decision, review_feedback = await runner.run_ci_fix_review(
            result or "", failures, pr_title,
        )
        if review_decision == "reject":
            _log("UNFIXABLE claim rejected — retrying fix")
            result = await runner.run_ci_fix_retry(review_feedback)
            result_upper = (result or "").upper()
        else:
            _log("CI failures confirmed unrelated to PR changes", "error")
            return False

    if "UNFIXABLE" in result_upper:
        _log("CI failures confirmed unrelated to PR changes", "error")
        return False

    if "DONE" in result_upper:
        new_sha = await git_get_current_sha(clone_path)
        if new_sha != old_sha:
            pushed = await git_push_force_with_lease(clone_path, branch)
            if not pushed:
                _log("Push rejected after CI fix", "error")
                return False
            new_commits = await git_get_new_commits_since(clone_path, old_sha)
            await state_manager.record_our_commits(
                repo, str(pr_number), new_commits,
            )
            _log("CI fix pushed")
        else:
            _log("CI fix: agent made no commits")
        return True

    _log("CI fix agent did not complete", "error")
    return False
