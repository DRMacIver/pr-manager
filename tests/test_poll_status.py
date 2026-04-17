"""Tests for the status-only poll helper."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pr_manager import poll as poll_module


@pytest.mark.asyncio
async def test_compute_pr_status_behind_takes_priority_over_checks():
    """A PR that is behind base is reported as `behind` even if checks pass."""
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=2)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "behind"


@pytest.mark.asyncio
async def test_compute_pr_status_maps_gh_green():
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "green"


@pytest.mark.asyncio
async def test_compute_pr_status_maps_gh_failing():
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("failing", "x"))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "failing"


@pytest.mark.asyncio
async def test_compute_pr_status_maps_no_checks():
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("no_checks", ""))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "no_checks"
