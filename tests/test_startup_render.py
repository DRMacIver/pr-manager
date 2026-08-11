"""Startup must render the last-known state immediately.

Regression: the table stayed empty until the first poll pass finished
(gh listing + pristine fetch + per-PR clone/status for every PR, all
network-bound) even though state.json already held everything needed
to paint the listing instantly. The poll then refreshes rows in place.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pr_manager import headless as headless_module
from pr_manager import tui as tui_module
from pr_manager.headless import run_headless
from pr_manager.state import PRState, StateManager
from pr_manager.tui import PRManagerApp


async def _seeded_manager() -> StateManager:
    sm = StateManager()
    await sm.load()
    await sm.add_repo("foo/bar")
    await sm.upsert_pr_state(
        "foo/bar", "7",
        PRState(title="cached pr", branch="feat", status="green",
                created_at="2026-01-01T00:00:00Z"),
    )
    await sm.add_local_branch("foo/bar", "wip")
    return sm


@pytest.mark.asyncio
async def test_tui_renders_persisted_state_before_first_poll(state_path):
    sm = await _seeded_manager()
    app = PRManagerApp(sm, poll_interval=5)

    # Poll loop patched to hang forever: only the cached render can
    # populate the table.
    async def never_finishes(*args, **kwargs):
        import asyncio
        await asyncio.Event().wait()

    with patch.object(tui_module, "poll_loop", never_finishes):
        async with app.run_test():
            table = app.query_one(tui_module.DataTable)
            assert table.row_count == 2, (
                "persisted PRs and local branches must be listed at mount, "
                "before any poll data arrives"
            )
            statuses = {p.number: p.status for p in app._display_prs}
            assert statuses[7] == "green", "last-known status must be shown"


@pytest.mark.asyncio
async def test_headless_prints_persisted_state_before_first_poll(state_path, capsys):
    sm = await _seeded_manager()

    with patch.object(headless_module, "poll_loop", AsyncMock()):
        await run_headless(sm, poll_interval=5)

    out = capsys.readouterr().out
    assert "cached pr" in out or "#   7" in out or "feat" in out, (
        f"headless startup must print the cached listing, got: {out!r}"
    )
