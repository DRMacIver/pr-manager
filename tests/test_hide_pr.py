"""Tests for the hide-PR feature.

Hiding a PR removes it from the displayed list permanently (across restarts)
without touching the local clone. The poll loop must skip hidden PRs when
seeding stub state, and stale entries (PRs no longer on GitHub) must be
cleaned up so the hidden list doesn't grow unbounded.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager.display import build_display_list
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
async def test_hide_pr_persists_across_load(state_path):
    sm = await _make_state_manager()
    await sm.upsert_pr_state("foo/bar", "42", PRState(title="hello", branch="x"))
    await sm.hide_pr("foo/bar", 42)

    assert await sm.is_hidden("foo/bar", 42)
    # Hiding also clears the cached pr_state so the row disappears immediately.
    assert await sm.get_pr_state("foo/bar", "42") is None

    sm2 = await _make_state_manager()
    assert await sm2.is_hidden("foo/bar", 42)
    assert await sm2.get_hidden_prs("foo/bar") == [42]


@pytest.mark.asyncio
async def test_hidden_pr_excluded_from_display_list(state_path):
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "1", PRState(title="visible", branch="a"))
    await sm.upsert_pr_state("foo/bar", "2", PRState(title="will-hide", branch="b"))

    display = await build_display_list(["foo/bar"], sm)
    assert {pr.number for pr in display} == {1, 2}

    await sm.hide_pr("foo/bar", 2)
    display = await build_display_list(["foo/bar"], sm)
    assert {pr.number for pr in display} == {1}


@pytest.mark.asyncio
async def test_unhide_restores_visibility(state_path):
    sm = await _make_state_manager()
    await sm.hide_pr("foo/bar", 7)
    assert await sm.is_hidden("foo/bar", 7)
    await sm.unhide_pr("foo/bar", 7)
    assert not await sm.is_hidden("foo/bar", 7)
    assert await sm.get_hidden_prs("foo/bar") == []


@pytest.mark.asyncio
async def test_poll_loop_skips_hidden_prs(state_path):
    """Hidden PRs from `gh pr list` must not get stub state or processor tasks."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.hide_pr("foo/bar", 99)

    fake_prs = [
        {"number": 1, "title": "keep", "headRefName": "k", "createdAt": "2026-01-01T00:00:00Z"},
        {"number": 99, "title": "hidden", "headRefName": "h", "createdAt": "2026-01-01T00:00:00Z"},
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

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise _Stop()

    class _Stop(Exception):
        pass

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=fake_prs)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "PRProcessor", processor_class),
        patch.object(poll_module.asyncio, "sleep", fake_sleep),
    ):
        try:
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5, recent_minutes=60)
        except _Stop:
            pass

    # Stub state should exist for #1 only.
    assert await sm.get_pr_state("foo/bar", "1") is not None
    assert await sm.get_pr_state("foo/bar", "99") is None
    # PRProcessor should never have been instantiated for the hidden PR.
    constructed_pr_numbers = [c.kwargs["pr_data"]["number"] for c in processor_class.call_args_list]
    assert 99 not in constructed_pr_numbers
    assert 1 in constructed_pr_numbers


@pytest.mark.asyncio
async def test_poll_loop_cleans_up_stale_hidden_entries(state_path):
    """If a hidden PR no longer appears in `gh pr list`, drop it from hidden_prs."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.hide_pr("foo/bar", 100)  # Hidden but no longer on GitHub.
    await sm.hide_pr("foo/bar", 101)  # Hidden and still on GitHub.

    fake_prs = [
        {"number": 101, "title": "still-here", "headRefName": "x", "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = MagicMock()
    host._active_tasks = {}

    async def fake_sleep(seconds: float) -> None:
        del seconds
        raise _Stop()

    class _Stop(Exception):
        pass

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=fake_prs)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "PRProcessor", MagicMock()),
        patch.object(poll_module.asyncio, "sleep", fake_sleep),
    ):
        try:
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5, recent_minutes=60)
        except _Stop:
            pass

    assert await sm.get_hidden_prs("foo/bar") == [101]


@pytest.mark.asyncio
async def test_state_file_format_includes_hidden_prs(state_path):
    sm = await _make_state_manager()
    await sm.hide_pr("foo/bar", 5)
    on_disk = json.loads(state_path.read_text())
    assert on_disk["hidden_prs"] == {"foo/bar": [5]}
