"""Safety properties of the poll loop.

Pins behavior that was previously only accidental:
- a gh failure must not trigger PR cleanup (else an API hiccup would
  treat every PR as closed and delete clones),
- a status refresh must preserve fields the fix process owns
  (session_id, our_commits),
- a mid-pass nudge must trigger an immediate re-poll (regression: the
  event was cleared after the pass, so a repo added while polling
  waited out the full interval),
- a status-check failure marks the PR 'error' rather than crashing the
  pass.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager.state import PRState, StateManager


class _Stop(Exception):
    pass


async def _stop_sleep(minutes, nudge) -> None:
    raise _Stop()


def _make_host():
    host = MagicMock()
    host._active_tasks = {}
    return host


async def _manager_with_pr() -> StateManager:
    sm = StateManager()
    await sm.load()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state(
        "foo/bar", "42",
        PRState(title="t", branch="feat", session_id="sess-1", our_commits=["abc"]),
    )
    return sm


def _pr_json(number: int = 42) -> dict:
    return {
        "number": number,
        "title": "t",
        "headRefName": "feat",
        "baseRefName": "main",
        "createdAt": "2026-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_gh_list_failure_cleans_up_nothing(state_path):
    sm = await _manager_with_pr()
    host = _make_host()
    removed: list = []

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(side_effect=RuntimeError("HTTP 502"))),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "remove_clone", lambda p: removed.append(p)),
        patch.object(poll_module, "_sleep_between_polls", _stop_sleep),
    ):
        with pytest.raises(_Stop):
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5)

    assert removed == [], "a gh API failure must never trigger clone cleanup"
    assert await sm.get_pr_state("foo/bar", "42") is not None


@pytest.mark.asyncio
async def test_status_refresh_preserves_fix_process_fields(state_path):
    """The poll upsert must not wipe session_id/our_commits, which the
    fix process owns — losing them breaks session resume."""
    sm = await _manager_with_pr()
    host = _make_host()

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=[_pr_json()])),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
        patch.object(poll_module, "_sleep_between_polls", _stop_sleep),
    ):
        with pytest.raises(_Stop):
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5)

    pr = await sm.get_pr_state("foo/bar", "42")
    assert pr is not None
    assert pr.status == "green"
    assert pr.session_id == "sess-1"
    assert pr.our_commits == ["abc"]


@pytest.mark.asyncio
async def test_status_check_failure_marks_pr_error(state_path):
    sm = await _manager_with_pr()
    host = _make_host()

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=[_pr_json()])),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "git_setup_pr_clone", AsyncMock(side_effect=RuntimeError("clone failed"))),
        patch.object(poll_module, "_sleep_between_polls", _stop_sleep),
    ):
        with pytest.raises(_Stop):
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5)

    pr = await sm.get_pr_state("foo/bar", "42")
    assert pr is not None and pr.status == "error"
    host.on_status_update.assert_called_with("foo/bar", 42, "error", None)


@pytest.mark.asyncio
async def test_nudge_set_during_pass_triggers_immediate_repoll(state_path):
    """Regression: nudge.clear() ran after the pass, so a nudge that
    arrived mid-pass (e.g. a repo added while polling) was wiped and the
    loop slept the full interval."""
    sm = await _manager_with_pr()
    host = _make_host()
    nudge = asyncio.Event()
    passes = 0
    sleeps = 0
    real_sleep = poll_module._sleep_between_polls

    async def fake_list_prs(repo):
        nonlocal passes
        passes += 1
        if passes == 1:
            nudge.set()  # something changed while we were polling
        return []

    async def stopping_sleep(minutes, nudge_arg):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            raise _Stop()
        await real_sleep(minutes, nudge_arg)

    with (
        patch.object(poll_module, "gh_list_prs", fake_list_prs),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "remove_clone", lambda p: True),
        patch.object(poll_module, "_sleep_between_polls", stopping_sleep),
    ):
        # With a 60-minute interval, only the nudge can bring the second
        # pass inside the 5s timeout.
        with pytest.raises(_Stop):
            await asyncio.wait_for(
                poll_module.poll_loop(
                    host, sm, poll_interval_minutes=60, nudge=nudge,
                ),
                timeout=5,
            )

    assert passes == 2
