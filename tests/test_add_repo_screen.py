"""Regression tests for the add-repo modal (and the new-branch modal's
matching bugs).

The report: pressing `a` broke all the styling, and although you could type
into the textbox, Enter did nothing and the Add button couldn't be selected.

Two defects:

* `Horizontal` defaults to `height: 1fr`. Inside the `height: auto` dialog,
  Textual 8 resolves that against the screen, so the buttons row (and with it
  the dialog and its thick border) expanded to fill the whole display.
* Neither input screen handled `Input.Submitted`, so Enter — the only
  affordance left with mouse capture disabled — did nothing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Input

from pr_manager import tui as tui_module
from pr_manager.tui import AddRepoScreen, NewBranchScreen
from tests.tui_helpers import mk_app


@pytest.mark.asyncio
async def test_add_repo_dialog_does_not_fill_the_screen():
    app = mk_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        screen = app.screen_stack[-1]
        assert isinstance(screen, AddRepoScreen)
        dialog = screen.query_one("#dialog")
        # One input + one row of buttons + padding/border: nowhere near 30.
        assert dialog.region.height < 15, dialog.region
        buttons = screen.query_one("#buttons")
        assert buttons.region.height <= 3, buttons.region


@pytest.mark.asyncio
async def test_enter_in_repo_input_adds_repo_and_dismisses():
    app = mk_app()
    app._state_manager.add_repo = AsyncMock()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        screen = app.screen_stack[-1]
        screen.query_one("#repo-input", Input).value = "foo/bar"
        await pilot.press("enter")
        await pilot.pause()
        app._state_manager.add_repo.assert_awaited_once_with("foo/bar")
        assert screen not in app.screen_stack


@pytest.mark.asyncio
async def test_enter_with_invalid_repo_keeps_screen_open():
    app = mk_app()
    app._state_manager.add_repo = AsyncMock()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        screen = app.screen_stack[-1]
        screen.query_one("#repo-input", Input).value = "not-a-repo"
        await pilot.press("enter")
        await pilot.pause()
        app._state_manager.add_repo.assert_not_awaited()
        assert screen in app.screen_stack


@pytest.mark.asyncio
async def test_new_branch_dialog_does_not_fill_the_screen(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/fake-tmux,1234,0")
    app = mk_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen_stack[-1]
        assert isinstance(screen, NewBranchScreen)
        # Title + two inputs + buttons row + padding/border = 16 rows.
        dialog = screen.query_one("#nb-dialog")
        assert dialog.region.height <= 16, dialog.region
        buttons = screen.query_one("#nb-buttons")
        assert buttons.region.height <= 3, buttons.region


@pytest.mark.asyncio
async def test_enter_in_branch_input_creates_branch(monkeypatch):
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
    monkeypatch.setenv("TMUX", "/tmp/fake-tmux,1234,0")

    app = mk_app()
    app._state_manager.get_repos = AsyncMock(return_value=["foo/bar"])
    app._state_manager.add_local_branch = AsyncMock()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen_stack[-1]
        assert isinstance(screen, NewBranchScreen)
        screen.query_one("#nb-repo", Input).value = "foo/bar"
        branch_input = screen.query_one("#nb-branch", Input)
        branch_input.value = "my-branch"
        branch_input.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app._state_manager.add_local_branch.assert_awaited_once_with("foo/bar", "my-branch")
        assert screen not in app.screen_stack
