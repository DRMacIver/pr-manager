"""Tests for hiding open PRs from the listing.

`x` on a PR row hides it (persisted); `h` toggles showing hidden rows
so they can be unhidden with `x` again. Hidden PRs are skipped by the
poll loop's per-PR status refresh (no clone/fetch work for them) but
still cleaned up when they close.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager import tui as tui_module
from pr_manager.display import build_display_list
from pr_manager.state import PRState, Settings, StateManager
from pr_manager.tui import PRManagerApp


async def _manager() -> StateManager:
    sm = StateManager()
    await sm.load()
    return sm


# ── state ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hidden_flag_round_trips(state_path):
    sm = await _manager()
    await sm.upsert_pr_state("foo/bar", "7", PRState(title="t", branch="b"))
    await sm.set_pr_hidden("foo/bar", "7", True)

    fresh = await _manager()
    pr = await fresh.get_pr_state("foo/bar", "7")
    assert pr is not None and pr.hidden is True

    await fresh.set_pr_hidden("foo/bar", "7", False)
    pr = await fresh.get_pr_state("foo/bar", "7")
    assert pr is not None and pr.hidden is False


@pytest.mark.asyncio
async def test_set_hidden_on_unknown_pr_is_noop(state_path):
    sm = await _manager()
    await sm.set_pr_hidden("foo/bar", "999", True)
    assert await sm.get_pr_state("foo/bar", "999") is None


# ── display ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_display_list_carries_hidden_flag(state_path):
    sm = await _manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "1", PRState(title="visible", branch="a"))
    await sm.upsert_pr_state(
        "foo/bar", "2", PRState(title="hidden", branch="b", hidden=True),
    )

    prs = await build_display_list(["foo/bar"], sm)

    flags = {p.number: p.hidden for p in prs}
    assert flags == {1: False, 2: True}


# ── poll loop ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_skips_status_refresh_for_hidden_prs(state_path):
    class _Stop(Exception):
        pass

    async def stop_sleep(minutes, nudge):
        raise _Stop()

    sm = await _manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state(
        "foo/bar", "1", PRState(title="hid", branch="a", hidden=True),
    )
    host = MagicMock()
    host._active_tasks = {}

    prs_json = [
        {"number": 1, "title": "hid", "headRefName": "a", "baseRefName": "main",
         "createdAt": "2026-01-01T00:00:00Z"},
        {"number": 2, "title": "vis", "headRefName": "b", "baseRefName": "main",
         "createdAt": "2026-01-01T00:00:00Z"},
    ]

    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=prs_json)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "git_setup_pr_clone", AsyncMock()) as setup,
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))) as checks,
        patch.object(poll_module, "_sleep_between_polls", stop_sleep),
    ):
        with pytest.raises(_Stop):
            await poll_module.poll_loop(host, sm, poll_interval_minutes=5)

    refreshed = [c.args[1] for c in setup.await_args_list]
    assert 2 in refreshed, "visible PRs must still be refreshed"
    assert 1 not in refreshed, "hidden PRs must not get clone/status work"
    assert checks.await_count == 1
    # But the hidden PR keeps its state (cleanup-on-close still needs it).
    assert await sm.get_pr_state("foo/bar", "1") is not None


# ── TUI ──────────────────────────────────────────────────────────────────────


def _mk_app(sm=None) -> PRManagerApp:
    if sm is None:
        sm = MagicMock()
        sm.get_settings = AsyncMock(return_value=Settings())
    return PRManagerApp(sm, poll_interval=5)


@pytest.mark.asyncio
async def test_x_hides_pr_and_h_reveals_it(state_path):
    sm = await _manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "7", PRState(title="t", branch="b"))

    app = _mk_app(sm)
    with patch.object(tui_module, "poll_loop", AsyncMock()):
        async with app.run_test():
            app._display_prs = await build_display_list(["foo/bar"], sm)
            app._refresh_table()
            table = app.query_one(tui_module.DataTable)
            assert table.row_count == 1

            # x hides the PR: row disappears, flag persisted.
            await app.action_toggle_disabled()
            assert table.row_count == 0
            pr = await sm.get_pr_state("foo/bar", "7")
            assert pr is not None and pr.hidden is True

            # h reveals hidden rows.
            app.action_toggle_show_hidden()
            assert table.row_count == 1

            # x on the revealed row unhides it.
            await app.action_toggle_disabled()
            pr = await sm.get_pr_state("foo/bar", "7")
            assert pr is not None and pr.hidden is False

            # ...and it stays listed once hidden rows are toggled off again.
            app.action_toggle_show_hidden()
            assert table.row_count == 1


@pytest.mark.asyncio
async def test_selection_maps_to_visible_rows(state_path):
    """With a hidden PR filtered out, the cursor must select what the
    user sees — not index into the unfiltered list."""
    sm = await _manager()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state("foo/bar", "2", PRState(title="hid", branch="a", hidden=True))
    await sm.upsert_pr_state("foo/bar", "1", PRState(title="vis", branch="b"))

    app = _mk_app(sm)
    with patch.object(tui_module, "poll_loop", AsyncMock()):
        async with app.run_test():
            app._display_prs = await build_display_list(["foo/bar"], sm)
            app._refresh_table()
            table = app.query_one(tui_module.DataTable)
            assert table.row_count == 1

            selected = app._get_selected_pr()
            assert selected is not None and selected.number == 1
