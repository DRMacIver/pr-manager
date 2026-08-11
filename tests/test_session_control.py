"""Tests for fix/claude session sentinel bookkeeping and interruption.

Regression: "interrupting" an automated agent only cancelled the
watch_tmux_window sentinel task — the actual `pr-manager fix` process in
its tmux window kept running, while the UI claimed it was stopped (and
the claude-window sentinel then overwrote the fix sentinel, hiding the
'fixing' overlay entirely). Sentinel dict entries were also never
removed once done.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import tui as tui_module
from pr_manager.assistant_api import AssistantContext

KEY = ("foo/bar", 42)


from .tui_helpers import mk_app as _app


@pytest.fixture
def killed_windows(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run_cmd(args, **kwargs):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(tui_module, "run_cmd", fake_run_cmd)
    return calls


async def _pending_task() -> asyncio.Task:
    return asyncio.create_task(asyncio.sleep(60))


@pytest.mark.asyncio
async def test_stop_session_kills_the_fix_tmux_window(killed_windows):
    app = _app()
    task = await _pending_task()
    app._active_tasks[KEY] = task
    app._session_windows[KEY] = "fix-42"

    stopped = await app.stop_session(KEY)

    assert stopped is True
    assert ["tmux", "kill-window", "-t", "fix-42"] in killed_windows, (
        "interrupting a fix must kill the tmux window running the fix "
        "process, not just the watcher task"
    )
    await asyncio.sleep(0)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_stop_session_leaves_interactive_claude_window_alive(killed_windows):
    app = _app()
    app._active_tasks[KEY] = await _pending_task()
    app._session_windows[KEY] = "claude-42"

    stopped = await app.stop_session(KEY)

    assert stopped is True
    assert not any(a[:2] == ["tmux", "kill-window"] for a in killed_windows), (
        "an interactive claude window belongs to the user — only the "
        "sentinel should be cancelled"
    )


@pytest.mark.asyncio
async def test_stop_session_without_active_task_is_noop(killed_windows):
    assert await _app().stop_session(KEY) is False


@pytest.mark.asyncio
async def test_sentinel_bookkeeping_cleared_when_window_closes(monkeypatch):
    async def instant_watch(window_name):
        return

    monkeypatch.setattr(tui_module, "watch_tmux_window", instant_watch)
    app = _app()

    sentinel = app._install_sentinel(KEY, "fix-42")
    assert app._active_tasks[KEY] is sentinel
    assert app._session_windows[KEY] == "fix-42"

    await sentinel
    await asyncio.sleep(0)  # let the done callback run

    assert KEY not in app._active_tasks
    assert KEY not in app._session_windows


@pytest.mark.asyncio
async def test_sentinel_completion_repaints_and_nudges_the_poll(monkeypatch):
    """When a session ends, the row must not stay frozen on 'fixing'
    until the next multi-minute poll: the table repaints immediately and
    the poll loop is nudged for a fresh status."""
    async def instant_watch(window_name):
        return

    monkeypatch.setattr(tui_module, "watch_tmux_window", instant_watch)
    app = _app()
    refreshes: list[int] = []

    with patch.object(tui_module, "poll_loop", AsyncMock()):
        async with app.run_test():
            monkeypatch.setattr(app, "_refresh_table", lambda: refreshes.append(1))
            app._poll_nudge.clear()
            sentinel = app._install_sentinel(KEY, "fix-42")
            await sentinel
            await asyncio.sleep(0)  # let the done callback run

            assert refreshes, "table must repaint when the session ends"
            assert app._poll_nudge.is_set(), "poll must be nudged for fresh status"


@pytest.mark.asyncio
async def test_cancel_agent_stops_the_session(killed_windows):
    app = _app()
    app._active_tasks[KEY] = await _pending_task()
    app._session_windows[KEY] = "fix-42"
    ctx = AssistantContext(app, MagicMock(), app._active_tasks)

    assert await ctx.cancel_agent("foo/bar", 42) is True
    assert ["tmux", "kill-window", "-t", "fix-42"] in killed_windows


@pytest.mark.asyncio
async def test_list_running_agents_reports_only_live_sessions():
    app = _app()
    live = await _pending_task()
    done = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0.01)
    app._active_tasks[("foo/bar", 1)] = live
    app._active_tasks[("foo/bar", 2)] = done
    app._session_windows[("foo/bar", 1)] = "fix-1"
    ctx = AssistantContext(app, MagicMock(), app._active_tasks)

    agents = ctx.list_running_agents()

    assert [(a["repo"], a["pr_number"]) for a in agents] == [("foo/bar", 1)]
    assert agents[0]["window"] == "fix-1"
    live.cancel()
