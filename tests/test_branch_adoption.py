"""Tests for local branch → PR adoption.

When a local branch gets a PR created for it, the poll loop should rename
the existing branch clone directory to the PR clone path rather than
leaving the old directory orphaned while the processor creates a new one.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager.git import get_branch_clone_path, get_clone_path
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
async def test_adopt_renames_branch_clone_to_pr_clone(state_path, repos_dir):
    """When a local branch gets a PR, the branch clone directory should be
    renamed to the PR clone path so the processor uses the existing work."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.add_local_branch("foo/bar", "my-feature")

    # Create the branch clone directory with some work in it.
    branch_clone = get_branch_clone_path("foo/bar", "my-feature")
    branch_clone.mkdir(parents=True)
    (branch_clone / "work.txt").write_text("important work in progress")

    # gh pr list now returns a PR for this branch.
    fake_prs = [
        {"number": 42, "title": "My feature", "headRefName": "my-feature",
         "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = MagicMock()
    host._active_tasks = {}
    host.on_log = MagicMock()
    host.on_status_update = MagicMock()
    host.on_pr_list = MagicMock()

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=fake_prs)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "PRProcessor", MagicMock()),
        patch.object(poll_module.asyncio, "sleep", _fake_sleep),
    ):
        try:
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5, recent_minutes=60)
        except _Stop:
            pass

    pr_clone = get_clone_path("foo/bar", 42)

    # The branch clone should have been renamed to the PR clone path.
    assert pr_clone.exists(), (
        "PR clone directory should exist after adoption"
    )
    assert (pr_clone / "work.txt").read_text() == "important work in progress", (
        "Work in the branch clone should be preserved in the PR clone"
    )
    assert not branch_clone.exists(), (
        "Branch clone directory should no longer exist after rename"
    )

    # The local branch should be removed from tracking.
    assert await sm.get_local_branches("foo/bar") == []


@pytest.mark.asyncio
async def test_adopt_skips_rename_if_pr_clone_already_exists(state_path, repos_dir):
    """If the PR clone already exists (e.g. from a prior run), don't
    clobber it — just remove the local branch from tracking."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.add_local_branch("foo/bar", "other-feat")

    # Both directories exist.
    branch_clone = get_branch_clone_path("foo/bar", "other-feat")
    branch_clone.mkdir(parents=True)
    (branch_clone / "branch.txt").write_text("branch work")

    pr_clone = get_clone_path("foo/bar", 10)
    pr_clone.mkdir(parents=True)
    (pr_clone / "pr.txt").write_text("pr work")

    fake_prs = [
        {"number": 10, "title": "Other", "headRefName": "other-feat",
         "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = MagicMock()
    host._active_tasks = {}
    host.on_log = MagicMock()
    host.on_status_update = MagicMock()
    host.on_pr_list = MagicMock()

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=fake_prs)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "PRProcessor", MagicMock()),
        patch.object(poll_module.asyncio, "sleep", _fake_sleep),
    ):
        try:
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5, recent_minutes=60)
        except _Stop:
            pass

    # PR clone should be untouched.
    assert (pr_clone / "pr.txt").read_text() == "pr work"
    # Local branch removed from tracking.
    assert await sm.get_local_branches("foo/bar") == []
