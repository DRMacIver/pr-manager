"""Tests for the disable-PR feature (replaces the old hide-PR feature).

Disabling a PR stops the processor from working on it but keeps it visible
in the TUI list with a "disabled" status.  The user can re-enable it with
the same 'x' key.
"""
from __future__ import annotations

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


class _Stop(Exception):
    pass


async def _fake_sleep(seconds: float) -> None:
    raise _Stop()


# ── State persistence ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disable_pr_persists_across_load(state_path):
    sm = await _make_state_manager()
    await sm.upsert_pr_state("foo/bar", "42", PRState(title="hello", branch="x"))
    await sm.disable_pr("foo/bar", 42)

    assert await sm.is_disabled("foo/bar", 42)

    # Disabling must NOT clear state — the PR should remain visible.
    assert await sm.get_pr_state("foo/bar", "42") is not None

    sm2 = await _make_state_manager()
    assert await sm2.is_disabled("foo/bar", 42)
    assert await sm2.get_disabled_prs("foo/bar") == [42]


@pytest.mark.asyncio
async def test_enable_restores_processing(state_path):
    sm = await _make_state_manager()
    await sm.disable_pr("foo/bar", 7)
    assert await sm.is_disabled("foo/bar", 7)
    await sm.enable_pr("foo/bar", 7)
    assert not await sm.is_disabled("foo/bar", 7)
    assert await sm.get_disabled_prs("foo/bar") == []


# ── Display list ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_pr_still_in_display_list(state_path):
    """Disabled PRs must still appear in the display list."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "1", PRState(title="visible", branch="a"))
    await sm.upsert_pr_state("foo/bar", "2", PRState(title="will-disable", branch="b"))

    await sm.disable_pr("foo/bar", 2)
    display = await build_display_list(["foo/bar"], sm)
    assert {pr.number for pr in display} == {1, 2}


# ── Poll loop: disabled PRs get stub state but no processor ────────────────


@pytest.mark.asyncio
async def test_poll_loop_skips_processor_for_disabled_prs(state_path):
    """Disabled PRs from ``gh pr list`` must get stub state but no processor."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.disable_pr("foo/bar", 99)

    fake_prs = [
        {"number": 1, "title": "keep", "headRefName": "k", "createdAt": "2026-01-01T00:00:00Z"},
        {"number": 99, "title": "disabled", "headRefName": "h", "createdAt": "2026-01-01T00:00:00Z"},
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

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=fake_prs)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "PRProcessor", processor_class),
        patch.object(poll_module.asyncio, "sleep", _fake_sleep),
    ):
        try:
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5, recent_minutes=60)
        except _Stop:
            pass

    # Stub state should exist for BOTH #1 and #99.
    assert await sm.get_pr_state("foo/bar", "1") is not None
    assert await sm.get_pr_state("foo/bar", "99") is not None
    # PRProcessor should only have been created for #1, not disabled #99.
    constructed_pr_numbers = [c.kwargs["pr_data"]["number"] for c in processor_class.call_args_list]
    assert 99 not in constructed_pr_numbers
    assert 1 in constructed_pr_numbers


# ── Cleanup: disabled PRs not treated as gone ──────────────────────────────


@pytest.mark.asyncio
async def test_poll_loop_does_not_clean_up_disabled_prs(state_path):
    """Disabled PRs must not have their clones or state removed."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "50", PRState(title="disabled-pr", branch="d"))
    await sm.disable_pr("foo/bar", 50)

    # PR #50 is still on GitHub:
    fake_prs = [
        {"number": 50, "title": "disabled-pr", "headRefName": "d", "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = MagicMock()
    host._active_tasks = {}
    host.on_log = MagicMock()
    host.on_status_update = MagicMock()
    host.on_pr_list = MagicMock()

    remove_clone_calls = []

    def tracking_remove_clone(p) -> bool:
        remove_clone_calls.append(p)
        return True

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=fake_prs)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "remove_clone", tracking_remove_clone),
        patch.object(poll_module, "PRProcessor", MagicMock()),
        patch.object(poll_module.asyncio, "sleep", _fake_sleep),
    ):
        try:
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5, recent_minutes=60)
        except _Stop:
            pass

    assert not remove_clone_calls, "remove_clone should not be called for disabled PRs"
    assert await sm.get_pr_state("foo/bar", "50") is not None


# ── Stale disabled entries cleaned up ──────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_loop_cleans_up_stale_disabled_entries(state_path):
    """If a disabled PR no longer appears in ``gh pr list``, drop it from
    disabled_prs so the list doesn't grow unbounded."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.disable_pr("foo/bar", 100)  # Disabled but no longer on GitHub.
    await sm.disable_pr("foo/bar", 101)  # Disabled and still on GitHub.

    fake_prs = [
        {"number": 101, "title": "still-here", "headRefName": "x", "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = MagicMock()
    host._active_tasks = {}

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

    assert await sm.get_disabled_prs("foo/bar") == [101]


# ── Backward compat: old hidden_prs key migrated to disabled_prs ──────────


@pytest.mark.asyncio
async def test_old_hidden_prs_migrated_on_load(state_path):
    """State files with the old 'hidden_prs' key should be loaded as
    disabled_prs."""
    import json
    state_path.write_text(json.dumps({
        "repos": ["foo/bar"],
        "pr_state": {},
        "hidden_prs": {"foo/bar": [42, 99]},
    }))

    sm = await _make_state_manager()
    assert await sm.get_disabled_prs("foo/bar") == [42, 99]
    assert await sm.is_disabled("foo/bar", 42)
