"""Tests for assorted TUI robustness fixes."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import tui as tui_module
from pr_manager.state import PRDisplayInfo, Settings
from pr_manager.tui import PRManagerApp, log_path_for, watch_tmux_window


def _pr(number: int = 42, branch: str = "feat") -> PRDisplayInfo:
    return PRDisplayInfo(
        repo="foo/bar", number=number, title="t", branch=branch,
        status="green", age="1h", error_message=None,
    )


def _mk_app() -> PRManagerApp:
    sm = MagicMock()
    sm.get_settings = AsyncMock(return_value=Settings())
    return PRManagerApp(sm, poll_interval=5)


# ── browser opening ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_browser_uses_webbrowser_module(monkeypatch):
    """Regression: the action ran the macOS-only `open` binary, which
    raises FileNotFoundError on Linux — the platform this tool ships on."""
    app = _mk_app()
    opened: list[str] = []
    monkeypatch.setattr(tui_module.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(app, "_get_selected_pr", lambda: _pr())

    with patch.object(tui_module, "poll_loop", AsyncMock()):
        async with app.run_test():
            await app.action_open_browser()

    assert opened == ["https://github.com/foo/bar/pull/42"]


# ── log paths for local branches ─────────────────────────────────────────────


def test_log_path_for_pr_uses_pr_number():
    assert log_path_for(_pr(number=42)).name == "pr-42.log"


def test_log_path_for_local_branch_is_branch_specific():
    """Regression: every local branch (number=0) mapped to the same
    meaningless pr-0.log."""
    a = log_path_for(_pr(number=0, branch="feature-a"))
    b = log_path_for(_pr(number=0, branch="feature-b"))
    assert a != b
    assert "feature-a" in a.name
    assert "pr-0" not in a.name


# ── tmux window watching ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watch_tmux_window_survives_transient_tmux_failure(monkeypatch):
    """Regression: one failed `tmux list-windows` (e.g. server restarting)
    made the sentinel exit, dropping the 'fixing' overlay while the fix
    was still running."""
    monkeypatch.setattr(tui_module, "_TMUX_WATCH_INTERVAL", 0)
    results = iter([
        (1, "", "server error"),          # transient failure
        (0, "fix-42\nother", ""),         # window still there
        (0, "other", ""),                 # window gone → exit
    ])

    async def fake_run_cmd(args, **kwargs):
        return next(results)

    monkeypatch.setattr(tui_module, "run_cmd", fake_run_cmd)
    await asyncio.wait_for(watch_tmux_window("fix-42"), timeout=5)


@pytest.mark.asyncio
async def test_watch_tmux_window_gives_up_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(tui_module, "_TMUX_WATCH_INTERVAL", 0)

    async def always_failing(args, **kwargs):
        return 1, "", "tmux is gone"

    monkeypatch.setattr(tui_module, "run_cmd", always_failing)
    await asyncio.wait_for(watch_tmux_window("fix-42"), timeout=5)


# ── NewBranchScreen double-submit guard ──────────────────────────────────────


@pytest.mark.asyncio
async def test_new_branch_create_ignores_double_submit(monkeypatch):
    from pr_manager.tui import NewBranchScreen

    sm = MagicMock()
    sm.get_repos = AsyncMock(return_value=["foo/bar"])
    sm.add_repo = AsyncMock()
    sm.add_local_branch = AsyncMock()
    sm.get_settings = AsyncMock(return_value=Settings())

    release = asyncio.Event()
    clone_calls: list[str] = []

    async def slow_clone(repo, branch):
        clone_calls.append(branch)
        await release.wait()
        return tui_module.get_branch_clone_path(repo, branch)

    monkeypatch.setattr(tui_module, "git_update_pristine", AsyncMock())
    monkeypatch.setattr(tui_module, "git_create_branch_clone", slow_clone)
    monkeypatch.setattr(tui_module, "run_cmd", AsyncMock(return_value=(0, "", "")))

    screen = NewBranchScreen(sm, ["foo/bar"])
    app = _mk_app()
    with patch.object(tui_module, "poll_loop", AsyncMock()):
        async with app.run_test():
            await app.push_screen(screen)
            screen.query_one("#nb-repo").value = "foo/bar"
            screen.query_one("#nb-branch").value = "my-branch"

            first = asyncio.ensure_future(screen._create())
            await asyncio.sleep(0.05)   # first submit now blocked in clone
            await screen._create()      # second submit must be ignored
            release.set()
            await first

    assert clone_calls == ["my-branch"], (
        f"double-submit started {len(clone_calls)} clones"
    )
