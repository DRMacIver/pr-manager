"""Tests for local branch → PR adoption.

When a local branch gets a PR created for it, git_setup_pr_clone should
detect the existing branch clone and symlink to it rather than creating
a fresh clone.  This way:
- Active processes in the branch clone are unaffected
- The PR clone path reuses the branch clone's working state
"""
from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from pr_manager.git import get_branch_clone_path, get_clone_path, git_setup_pr_clone


@pytest.mark.asyncio
async def test_setup_pr_clone_symlinks_to_existing_branch_clone(repos_dir):
    """If a branch clone already exists for this PR's branch,
    git_setup_pr_clone should create a symlink rather than a fresh clone."""
    branch_clone = get_branch_clone_path("foo/bar", "my-feature")
    branch_clone.mkdir(parents=True)
    (branch_clone / "work.txt").write_text("important work")

    # git_setup_pr_clone should detect the branch clone and symlink.
    with patch("pr_manager.git._clone_from_pristine", AsyncMock()) as mock_clone:
        await git_setup_pr_clone("foo/bar", 42, "my-feature")
        # Should NOT have cloned from pristine.
        mock_clone.assert_not_called()

    pr_clone = get_clone_path("foo/bar", 42)
    assert pr_clone.is_symlink(), (
        "PR clone should be a symlink to the branch clone"
    )
    assert pr_clone.resolve() == branch_clone.resolve(), (
        "PR clone symlink should point to the branch clone"
    )
    assert (pr_clone / "work.txt").read_text() == "important work"


@pytest.mark.asyncio
async def test_setup_pr_clone_creates_fresh_when_no_branch_clone(repos_dir):
    """If no branch clone exists, git_setup_pr_clone should create a fresh
    clone as before."""
    with (
        patch("pr_manager.git._clone_from_pristine", AsyncMock()) as mock_clone,
        patch("pr_manager.git.run_cmd", AsyncMock()) as mock_run,
    ):
        await git_setup_pr_clone("foo/bar", 42, "my-feature")
        mock_clone.assert_called_once()


@pytest.mark.asyncio
async def test_setup_pr_clone_fetches_and_checks_out_pr_branch(repos_dir):
    """A fresh PR clone must fetch from origin and then check out the PR
    branch.

    Regression test: the clone is created from the local pristine cache,
    whose remote-tracking refs are NOT carried over as branches.  Without an
    explicit fetch the PR branch is absent, and a silent (check=False)
    checkout leaves the clone sitting on the default branch (main).  A later
    `git rebase origin/main` then rebases main itself instead of the PR
    branch — corrupting the local default branch and attempting a push that
    branch protection rejects.
    """
    clone_path = get_clone_path("foo/bar", 42)

    with (
        patch("pr_manager.git._clone_from_pristine", AsyncMock()) as mock_clone,
        patch("pr_manager.git.run_cmd", AsyncMock(return_value=(0, "", ""))) as mock_run,
    ):
        await git_setup_pr_clone("foo/bar", 42, "my-feature")
        mock_clone.assert_called_once()

    # Must fetch from origin so origin/my-feature exists before checkout.
    assert (
        call(["git", "fetch", "origin", "--prune"], cwd=clone_path)
        in mock_run.call_args_list
    ), "git_setup_pr_clone must fetch from origin before checking out the PR branch"

    # The checkout must happen, must target the PR branch, and must NOT be
    # check=False — a failure to land on the PR branch must raise rather than
    # silently leave the clone on main.
    checkout_calls = [
        c for c in mock_run.call_args_list
        if c.args and c.args[0][:2] == ["git", "checkout"]
    ]
    assert checkout_calls == [
        call(["git", "checkout", "my-feature"], cwd=clone_path)
    ], f"unexpected checkout calls: {checkout_calls}"

    # The fetch must come before the checkout.
    fetch_idx = next(
        i for i, c in enumerate(mock_run.call_args_list)
        if c.args and c.args[0][:2] == ["git", "fetch"]
    )
    checkout_idx = next(
        i for i, c in enumerate(mock_run.call_args_list)
        if c.args and c.args[0][:2] == ["git", "checkout"]
    )
    assert fetch_idx < checkout_idx, "fetch must precede checkout"


@pytest.mark.asyncio
async def test_setup_pr_clone_skips_if_pr_clone_already_exists(repos_dir):
    """If the PR clone already exists, do nothing."""
    pr_clone = get_clone_path("foo/bar", 42)
    pr_clone.mkdir(parents=True)
    (pr_clone / "existing.txt").write_text("already here")

    with patch("pr_manager.git._clone_from_pristine", AsyncMock()) as mock_clone:
        await git_setup_pr_clone("foo/bar", 42, "my-feature")
        mock_clone.assert_not_called()

    # Should be a real directory, not a symlink.
    assert not pr_clone.is_symlink()
    assert (pr_clone / "existing.txt").read_text() == "already here"


@pytest.mark.asyncio
async def test_setup_pr_clone_replaces_stale_symlink(repos_dir):
    """If the PR clone path is a dangling symlink (target deleted), it should
    be removed and a fresh clone created."""
    # Create and then break a symlink
    branch_clone = get_branch_clone_path("foo/bar", "old-feature")
    branch_clone.mkdir(parents=True)
    pr_clone = get_clone_path("foo/bar", 42)
    pr_clone.symlink_to(branch_clone.resolve())
    branch_clone.rmdir()

    assert not pr_clone.exists()  # broken symlink
    assert pr_clone.is_symlink()  # but symlink itself is present

    with (
        patch("pr_manager.git._clone_from_pristine", AsyncMock()) as mock_clone,
        patch("pr_manager.git.run_cmd", AsyncMock()) as mock_run,
    ):
        await git_setup_pr_clone("foo/bar", 42, "new-feature")
        mock_clone.assert_called_once()

    # The dangling symlink should have been cleaned up
    assert not pr_clone.is_symlink() or pr_clone.exists()
