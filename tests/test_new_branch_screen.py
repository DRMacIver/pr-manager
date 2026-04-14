"""Regression tests for the NewBranchScreen dismiss race.

The bug: `_create` calls `self.dismiss()` after several `await` points. If the
screen is dismissed during one of those awaits (e.g. user presses Escape while
the git clone is running), the final dismiss raises ScreenStackError because
the screen is no longer on the stack.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input

from pr_manager import tui as tui_module
from pr_manager.state import Settings
from pr_manager.tui import NewBranchScreen


class _Harness(App):
    def __init__(self, screen: NewBranchScreen) -> None:
        super().__init__()
        self._initial_screen = screen

    def compose(self) -> ComposeResult:  # pragma: no cover - textual plumbing
        return []

    async def on_mount(self) -> None:
        await self.push_screen(self._initial_screen)


def _mock_state_manager() -> AsyncMock:
    sm = AsyncMock()
    sm.get_repos = AsyncMock(return_value=["foo/bar"])
    sm.add_repo = AsyncMock()
    sm.add_local_branch = AsyncMock()
    sm.get_settings = AsyncMock(return_value=Settings())
    return sm


@pytest.mark.asyncio
async def test_create_does_not_crash_if_dismissed_mid_flight(monkeypatch):
    """If the user dismisses the modal while _create's awaits are in flight,
    the eventual `self.dismiss()` must not raise ScreenStackError."""

    gate = asyncio.Event()
    resumed = asyncio.Event()

    async def slow_update_pristine(repo):
        del repo
        await gate.wait()
        resumed.set()

    async def fast_create_branch(repo, branch):
        del repo, branch
        return Path("/tmp/fake-clone")

    async def fake_run_cmd(*args, **kwargs):
        del args, kwargs
        return 0, "", ""

    monkeypatch.setattr(tui_module, "git_update_pristine", slow_update_pristine)
    monkeypatch.setattr(tui_module, "git_create_branch_clone", fast_create_branch)
    monkeypatch.setattr(tui_module, "run_cmd", fake_run_cmd)

    sm = _mock_state_manager()
    screen = NewBranchScreen(sm, ["foo/bar"])
    app = _Harness(screen)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen.query_one("#nb-branch", Input).value = "my-branch"
        # Kick off _create — it will block on `gate` inside git_update_pristine.
        create_task = asyncio.create_task(screen._create())
        await pilot.pause()
        # Simulate the user pressing Escape mid-flight.
        screen.dismiss()
        await pilot.pause()
        # Now release the slow git op and let _create resume to its `self.dismiss()`.
        gate.set()
        # _create should complete without raising. If the bug is present, it
        # raises ScreenStackError when calling self.dismiss() at the end.
        await asyncio.wait_for(create_task, timeout=2.0)
        assert resumed.is_set()


@pytest.mark.asyncio
async def test_create_happy_path_dismisses_screen(monkeypatch):
    """If nothing else interferes, _create finishes by dismissing the modal."""

    async def fake_update_pristine(repo):
        del repo

    async def fake_create_branch(repo, branch):
        del repo, branch
        return Path("/tmp/fake-clone")

    async def fake_run_cmd(*args, **kwargs):
        del args, kwargs
        return 0, "", ""

    monkeypatch.setattr(tui_module, "git_update_pristine", fake_update_pristine)
    monkeypatch.setattr(tui_module, "git_create_branch_clone", fake_create_branch)
    monkeypatch.setattr(tui_module, "run_cmd", fake_run_cmd)

    sm = _mock_state_manager()
    screen = NewBranchScreen(sm, ["foo/bar"])
    app = _Harness(screen)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen in app.screen_stack
        screen.query_one("#nb-branch", Input).value = "my-branch"
        await screen._create()
        await pilot.pause()
        assert screen not in app.screen_stack
