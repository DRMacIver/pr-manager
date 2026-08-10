"""Tests for gh_pr_check_status classification.

Regression: only FAILURE used to count as failing and only
IN_PROGRESS/QUEUED/PENDING/WAITING as pending — every other state
(ERROR, CANCELLED, TIMED_OUT, ACTION_REQUIRED, STARTUP_FAILURE, STALE, …)
fell through to "green", so `pr-manager fix` declared success on broken CI
and the poll display showed green for timed-out or errored checks.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from pr_manager.git import gh_pr_check_status


async def _classify(states: list[str]) -> tuple[str, str]:
    checks = [{"name": f"check-{i}", "state": s} for i, s in enumerate(states)]
    with patch(
        "pr_manager.git.run_cmd",
        AsyncMock(return_value=(0, json.dumps(checks), "")),
    ):
        return await gh_pr_check_status("foo/bar", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "states",
    [
        ["SUCCESS"],
        ["SUCCESS", "SKIPPED"],
        ["SUCCESS", "NEUTRAL", "SKIPPED"],
    ],
)
async def test_passing_states_are_green(states):
    status, _details = await _classify(states)
    assert status == "green"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_state",
    [
        "FAILURE",
        "ERROR",
        "CANCELLED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "STALE",
    ],
)
async def test_unsuccessful_states_are_failing(bad_state):
    status, details = await _classify(["SUCCESS", bad_state])
    assert status == "failing"
    assert bad_state in details, "details must name the failing state"


@pytest.mark.asyncio
async def test_unknown_state_is_not_green():
    """A state this code has never heard of must never read as success."""
    status, _details = await _classify(["SUCCESS", "SOMETHING_NEW"])
    assert status == "failing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "running_state",
    ["IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED", "EXPECTED"],
)
async def test_running_states_are_pending(running_state):
    status, _details = await _classify(["SUCCESS", running_state])
    assert status == "pending"


@pytest.mark.asyncio
async def test_failure_takes_priority_over_pending():
    status, _details = await _classify(["IN_PROGRESS", "FAILURE"])
    assert status == "failing"


@pytest.mark.asyncio
async def test_empty_check_list_is_no_checks():
    status, _details = await _classify([])
    assert status == "no_checks"


@pytest.mark.asyncio
async def test_gh_failure_with_no_checks_message_is_no_checks():
    with patch(
        "pr_manager.git.run_cmd",
        AsyncMock(return_value=(1, "", "no checks reported on the 'x' branch")),
    ):
        status, _details = await gh_pr_check_status("foo/bar", 1)
    assert status == "no_checks"


@pytest.mark.asyncio
async def test_unexpected_gh_failure_is_error_with_details():
    """An auth/network failure must not masquerade as 'pending' forever;
    it surfaces as 'error' with the stderr preserved."""
    with patch(
        "pr_manager.git.run_cmd",
        AsyncMock(return_value=(1, "", "HTTP 401: Bad credentials")),
    ):
        status, details = await gh_pr_check_status("foo/bar", 1)
    assert status == "error"
    assert "Bad credentials" in details
