"""Tests for PR cleanup safety.

The poll loop removes state and clones for PRs that are no longer in the
``gh pr list`` response.  This cleanup has two bugs and a missing safety net:

1. Hidden PRs are filtered out of ``prs`` *before* ``current_numbers`` is
   built, so a hidden-but-open PR gets its clone deleted.
2. ``gh pr list`` defaults to 30 results — if the user has more open PRs,
   the extras are silently treated as closed and nuked.
3. ``remove_clone`` unconditionally calls ``shutil.rmtree`` with no sanity
   check on how recently the directory was used.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager.git import remove_clone
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


def _make_host():
    host = MagicMock()
    host._active_tasks = {}
    host.on_log = MagicMock()
    host.on_status_update = MagicMock()
    host.on_pr_list = MagicMock()
    return host


async def _fake_sleep(seconds: float) -> None:
    raise _Stop()


# ── Hidden-PR cleanup bug ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_pr_clone_not_deleted_when_still_on_github(state_path, tmp_path):
    """A PR that is disabled but still open on GitHub must NOT have its clone
    deleted by the cleanup loop."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "50", PRState(title="disabled-open", branch="h"))
    await sm.disable_pr("foo/bar", 50)

    # PR #50 is still on GitHub:
    fake_prs = [
        {"number": 50, "title": "disabled-open", "headRefName": "h", "createdAt": "2026-01-01T00:00:00Z"},
        {"number": 51, "title": "visible", "headRefName": "v", "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = _make_host()
    remove_clone_calls: list[Path] = []

    def tracking_remove_clone(p: Path) -> bool:
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

    # The clone for disabled PR #50 must NOT have been removed.
    clone_50 = poll_module.get_clone_path("foo/bar", 50)
    assert clone_50 not in remove_clone_calls, (
        f"remove_clone was called for disabled PR #50 — "
        f"disabled PRs still on GitHub must not be cleaned up"
    )


# ── mtime safety net ───────────────────────────────────────────────────────


def test_remove_clone_refuses_to_delete_recently_modified_directory(tmp_path):
    """remove_clone must not delete a directory whose mtime is less than a day
    old.  This is a safety net against incorrect cleanup."""
    clone = tmp_path / "pr-42"
    clone.mkdir()
    (clone / "file.txt").write_text("important work")
    # Directory was just created — mtime is now.

    remove_clone(clone)

    assert clone.exists(), (
        "remove_clone deleted a directory that was modified less than a day ago"
    )


def test_remove_clone_deletes_old_directory(tmp_path):
    """remove_clone should still delete directories older than a day."""
    clone = tmp_path / "pr-99"
    clone.mkdir()
    (clone / "file.txt").write_text("stale")

    # Backdate the mtime to 2 days ago.
    old_time = time.time() - 2 * 86400
    import os
    os.utime(clone, (old_time, old_time))

    remove_clone(clone)

    assert not clone.exists(), (
        "remove_clone should delete directories older than a day"
    )


@pytest.mark.asyncio
async def test_cleanup_does_not_remove_clone_for_pr_missing_from_gh_list(
    state_path, tmp_path, monkeypatch,
):
    """If a PR has state but is missing from ``gh pr list`` (e.g. due to the
    30-result default limit), and the clone was recently modified, it must NOT
    be deleted."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "188", PRState(title="my-pr", branch="feat"))

    # gh pr list returns only PR #1, not #188.
    fake_prs = [
        {"number": 1, "title": "other", "headRefName": "o", "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = _make_host()
    remove_clone_calls: list[Path] = []

    def tracking_remove_clone(p: Path) -> None:
        remove_clone_calls.append(p)

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

    # remove_clone should have been called, but since the directory would be
    # recent, it should have been a no-op (the real safety is in remove_clone
    # itself).  But importantly, the state should NOT be removed if the clone
    # was preserved.
    # For this test we just verify remove_clone WAS called (the poll loop still
    # tries) — the actual protection is in remove_clone's mtime check tested
    # separately above.  But the state removal should also be gated.
    # Actually, the better fix is: poll loop should pass info to remove_clone
    # and only remove state if the clone was actually deleted.
    # Let's test the end-to-end: state should survive if clone survives.
    # Since tracking_remove_clone is a no-op (doesn't actually delete), state
    # removal should be conditional on the clone being gone.
    # This test will initially fail because the current code unconditionally
    # removes state.
    pr_state = await sm.get_pr_state("foo/bar", "188")
    assert pr_state is not None, (
        "PR state was removed even though the clone was not deleted — "
        "state removal should be conditional on clone deletion"
    )
