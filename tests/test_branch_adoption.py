"""Tests for local branch → PR adoption.

When a local branch gets a PR created for it, git_setup_pr_clone should
detect the existing branch clone and symlink to it rather than creating
a fresh clone.  This way:
- Active processes in the branch clone are unaffected
- The PR clone path reuses the branch clone's working state
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager.git import get_branch_clone_path, get_clone_path, git_setup_pr_clone
from pr_manager.state import PRState, StateManager


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr("pr_manager.state.STATE_PATH", path)
    return path


@pytest.fixture
def repos_dir(tmp_path, monkeypatch):
    d = tmp_path / "repos"
    d.mkdir()
    monkeypatch.setattr("pr_manager.git.REPOS_DIR", d)
    return d


async def _make_state_manager() -> StateManager:
    sm = StateManager()
    await sm.load()
    return sm


class _Stop(Exception):
    pass


async def _fake_sleep(seconds: float) -> None:
    raise _Stop()


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
