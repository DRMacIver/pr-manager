"""Tests for git_create_branch_clone.

Verifies that creating a new branch clone fetches from origin before
checking out, so the branch is based on the latest default branch.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest

from pr_manager.git import git_create_branch_clone, git_default_branch


@pytest.fixture
def repos_dir(tmp_path, monkeypatch):
    d = tmp_path / "repos"
    d.mkdir()
    monkeypatch.setattr("pr_manager.git.REPOS_DIR", d)
    return d


@pytest.mark.asyncio
async def test_create_branch_clone_fetches_before_checkout(repos_dir):
    """git_create_branch_clone must fetch from origin after cloning from
    pristine and before creating the branch, so the branch is based on the
    latest default branch."""
    clone_path = repos_dir / "foo-bar" / "branch-my-feature"

    async def fake_run_cmd(args, cwd=None, check=True):
        if args[:3] == ["git", "rev-parse", "--verify"]:
            ref = args[3]
            if ref == "origin/main":
                return (0, "", "")
        return (0, "", "")

    with (
        patch("pr_manager.git._clone_from_pristine", AsyncMock()) as mock_clone,
        patch("pr_manager.git.run_cmd", AsyncMock(side_effect=fake_run_cmd)) as mock_run,
    ):
        result = await git_create_branch_clone("foo/bar", "my-feature")

    mock_clone.assert_called_once_with("foo/bar", clone_path)
    assert result == clone_path

    assert mock_run.call_args_list[-1] == call(
        ["git", "checkout", "-b", "my-feature", "origin/main"], cwd=clone_path
    )


@pytest.mark.asyncio
async def test_create_branch_clone_falls_back_to_master(repos_dir):
    """When origin/main doesn't exist, falls back to origin/master."""
    clone_path = repos_dir / "foo-bar" / "branch-my-feature"

    async def fake_run_cmd(args, cwd=None, check=True):
        if args[:3] == ["git", "rev-parse", "--verify"]:
            ref = args[3]
            if ref == "origin/main":
                return (1, "", "")
        return (0, "", "")

    with (
        patch("pr_manager.git._clone_from_pristine", AsyncMock()),
        patch("pr_manager.git.run_cmd", AsyncMock(side_effect=fake_run_cmd)) as mock_run,
    ):
        result = await git_create_branch_clone("foo/bar", "my-feature")

    assert result == clone_path
    assert mock_run.call_args_list[-1] == call(
        ["git", "checkout", "-b", "my-feature", "origin/master"], cwd=clone_path
    )


@pytest.mark.asyncio
async def test_git_default_branch_prefers_main():
    """git_default_branch returns 'main' when origin/main exists."""
    async def fake_run_cmd(args, cwd=None, check=True):
        return (0, "", "")

    with patch("pr_manager.git.run_cmd", AsyncMock(side_effect=fake_run_cmd)):
        assert await git_default_branch(Path("/fake")) == "main"


@pytest.mark.asyncio
async def test_git_default_branch_falls_back_to_master():
    """git_default_branch returns 'master' when origin/main doesn't exist."""
    async def fake_run_cmd(args, cwd=None, check=True):
        if args == ["git", "rev-parse", "--verify", "origin/main"]:
            return (1, "", "")
        return (0, "", "")

    with patch("pr_manager.git.run_cmd", AsyncMock(side_effect=fake_run_cmd)):
        assert await git_default_branch(Path("/fake")) == "master"
