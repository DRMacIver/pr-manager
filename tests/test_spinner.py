"""Tests for spinner/table refresh behavior.

Regression: the 0.12s spinner interval called _refresh_table, which
cleared and rebuilt every DataTable row ~8x/sec forever — even with no
active session — burning CPU and resetting the scroll position.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import tui as tui_module
from pr_manager.state import PRDisplayInfo, Settings
from pr_manager.tui import PRManagerApp


def _mk_app() -> PRManagerApp:
    sm = MagicMock()
    sm.get_settings = AsyncMock(return_value=Settings())
    return PRManagerApp(sm, poll_interval=5)


def _pr(number: int) -> PRDisplayInfo:
    return PRDisplayInfo(
        repo="foo/bar",
        number=number,
        title=f"PR {number}",
        branch=f"branch-{number}",
        status="green",
        age="1h",
        error_message=None,
    )


@pytest.mark.asyncio
async def test_spinner_tick_is_noop_when_idle_and_never_rebuilds():
    app = _mk_app()
    with patch.object(tui_module, "poll_loop", AsyncMock()):
        async with app.run_test():
            app._display_prs = [_pr(1), _pr(2)]
            app._refresh_table()

            table = app.query_one(tui_module.DataTable)
            with patch.object(table, "clear", wraps=table.clear) as clear:
                # Idle: no active sessions — the tick must do nothing.
                idx_before = app._spinner_idx
                app._tick_spinner()
                assert app._spinner_idx == idx_before
                clear.assert_not_called()

                # Active session: the spinner advances and updates cells,
                # but never clears/rebuilds the table.
                task = asyncio.create_task(asyncio.sleep(60))
                try:
                    app._active_tasks[("foo/bar", 1)] = task
                    app._tick_spinner()
                    assert app._spinner_idx != idx_before
                    clear.assert_not_called()
                finally:
                    task.cancel()
