"""Tests for git_create_branch_clone.

Verifies that creating a new branch clone fetches from origin before
checking out, so the branch is based on the latest origin/main.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest

from pr_manager.git import git_create_branch_clone


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
    latest origin/main."""
    clone_path = repos_dir / "foo-bar" / "branch-my-feature"

    with (
        patch("pr_manager.git._clone_from_pristine", AsyncMock()) as mock_clone,
        patch("pr_manager.git.run_cmd", AsyncMock(return_value=(0, "", ""))) as mock_run,
    ):
        result = await git_create_branch_clone("foo/bar", "my-feature")

    mock_clone.assert_called_once_with("foo/bar", clone_path)
    assert result == clone_path

    # Verify the calls: fetch first, then checkout.
    assert mock_run.call_count == 2
    fetch_call, checkout_call = mock_run.call_args_list

    assert fetch_call == call(
        ["git", "fetch", "origin", "--prune"], cwd=clone_path
    )
    assert checkout_call == call(
        ["git", "checkout", "-b", "my-feature", "origin/main"], cwd=clone_path
    )
