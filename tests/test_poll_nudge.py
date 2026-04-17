"""Tests for the poll-loop nudge mechanism.

An `asyncio.Event` passed to `poll_loop` can be set from elsewhere in
the app (e.g. after adopting a new local branch) to wake the loop up
immediately rather than waiting for the full sleep interval.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager.state import StateManager


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr("pr_manager.state.STATE_PATH", path)
    return path


async def _make_state_manager() -> StateManager:
    sm = StateManager()
    await sm.load()
    return sm


@pytest.mark.asyncio
async def test_nudge_event_interrupts_poll_sleep(state_path):
    """Setting the nudge event should wake the poll loop from its sleep
    and trigger an immediate poll cycle."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")

    fake_prs = [
        {"number": 1, "title": "pr", "headRefName": "b", "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = MagicMock()
    host._active_tasks = {}
    host.on_log = MagicMock()
    host.on_status_update = MagicMock()
    host.on_pr_list = MagicMock()

    nudge = asyncio.Event()
    poll_cycle_times: list[float] = []

    async def tracking_gh_list_prs(repo):
        poll_cycle_times.append(time.monotonic())
        if len(poll_cycle_times) == 1:
            # After first cycle sleeps, nudge should wake it.
            asyncio.get_event_loop().call_later(0.05, nudge.set)
        return fake_prs

    with (
        patch.object(poll_module, "gh_list_prs", tracking_gh_list_prs),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
    ):
        # Run with a 60-minute poll interval.  If nudge works, cycle 2
        # arrives in <1s.  If not, the 2-second timeout catches it.
        task = asyncio.create_task(
            poll_module.poll_loop(
                host, sm, poll_interval_minutes=60, recent_minutes=60,
                nudge=nudge,
            )
        )
        try:
            # Wait until we see at least 2 cycles, or give up after 2s.
            deadline = time.monotonic() + 2.0
            while len(poll_cycle_times) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert len(poll_cycle_times) >= 2, (
        f"Expected at least 2 poll cycles but got {len(poll_cycle_times)} — "
        f"nudge didn't wake the loop from its 60-minute sleep"
    )
    gap = poll_cycle_times[1] - poll_cycle_times[0]
    assert gap < 1.0, (
        f"Gap between poll cycles was {gap:.2f}s — expected <1s with nudge"
    )


@pytest.mark.asyncio
async def test_add_repo_sets_registered_nudge_event(state_path):
    """Adding a repo should set the registered nudge event so the poll
    loop notices the new repo without waiting for the next interval."""
    sm = await _make_state_manager()
    nudge = asyncio.Event()
    sm.set_nudge(nudge)

    assert not nudge.is_set()
    await sm.add_repo("foo/bar")
    assert nudge.is_set(), "add_repo should set the registered nudge event"


@pytest.mark.asyncio
async def test_add_repo_without_nudge_does_not_crash(state_path):
    """When no nudge is registered (e.g. the CLI `add` command) add_repo
    must still work — set_nudge is optional."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    assert "foo/bar" in await sm.get_repos()
