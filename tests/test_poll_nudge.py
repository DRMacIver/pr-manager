"""Tests for the poll-loop nudge mechanism.

When the user re-enables a disabled PR, the poll loop should wake up
immediately rather than waiting for the full sleep interval.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager.state import PRState, StateManager


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

    processor_class = MagicMock()
    processor_instance = MagicMock()
    processor_instance.process = AsyncMock()
    processor_class.return_value = processor_instance

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
        patch.object(poll_module, "PRProcessor", processor_class),
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
