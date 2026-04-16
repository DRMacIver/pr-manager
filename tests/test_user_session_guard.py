"""Tests for user Claude session protection.

When the user opens an interactive Claude session (via 'c' key), the poll loop
must not start automated agent work on that PR until the user's session ends
(i.e. the tmux window closes).

Two layers of protection:

1. **Sentinel task** (tui.py): action_open_claude_session stores a task in
   _active_tasks that stays alive while the tmux window exists. The poll loop's
   existing active-task check then skips the PR.

2. **Session-file check** (processor.py): before doing any automated work, the
   processor scans ``~/.claude/sessions/*.json`` for a live Claude process whose
   cwd matches the PR clone path. This survives pr-manager crashes because the
   session files are managed by Claude, not by pr-manager.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import poll as poll_module
from pr_manager import tui as tui_module
from pr_manager.processor import has_active_claude_session
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
async def test_poll_loop_skips_pr_with_user_session_sentinel(state_path):
    """A non-done sentinel task in _active_tasks must prevent poll_loop from
    spawning a PRProcessor for that PR."""
    sm = await _make_state_manager()
    await sm.add_repo("foo/bar")

    fake_prs = [
        {"number": 10, "title": "user session open", "headRefName": "a", "createdAt": "2026-01-01T00:00:00Z"},
        {"number": 20, "title": "no session", "headRefName": "b", "createdAt": "2026-01-01T00:00:00Z"},
    ]

    host = MagicMock()
    sentinel = asyncio.get_event_loop().create_future()  # never resolved → not done
    host._active_tasks = {("foo/bar", 10): sentinel}
    host.on_log = MagicMock()
    host.on_status_update = MagicMock()
    host.on_pr_list = MagicMock()

    processor_class = MagicMock()
    processor_instance = MagicMock()
    processor_instance.process = AsyncMock()
    processor_class.return_value = processor_instance

    class _Stop(Exception):
        pass

    async def fake_sleep(seconds):
        raise _Stop()

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

    constructed = [c.kwargs["pr_data"]["number"] for c in processor_class.call_args_list]
    assert 10 not in constructed, "PR with active user session must be skipped"
    assert 20 in constructed, "PR without user session should be processed"


@pytest.mark.asyncio
async def test_watch_tmux_window_completes_when_window_disappears(monkeypatch):
    """_watch_tmux_window should return once the window is no longer listed."""
    call_count = 0

    async def fake_run_cmd(cmd, check=True):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 0, "claude-42\nother-win\n", ""
        return 0, "other-win\n", ""

    monkeypatch.setattr(tui_module, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await tui_module.watch_tmux_window("claude-42")
    assert call_count == 2


@pytest.mark.asyncio
async def test_watch_tmux_window_stays_alive_while_window_exists(monkeypatch):
    """The watcher must keep looping while the window is listed."""
    call_count = 0

    async def fake_run_cmd(cmd, check=True):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return 0, "claude-42\n", ""
        return 0, "\n", ""

    monkeypatch.setattr(tui_module, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await tui_module.watch_tmux_window("claude-42")
    assert call_count == 4


@pytest.mark.asyncio
async def test_watch_tmux_window_completes_on_tmux_error(monkeypatch):
    """If tmux is gone (rc != 0), the watcher should return immediately."""

    async def failing_run_cmd(cmd, check=True):
        return 1, "", "no server running"

    monkeypatch.setattr(tui_module, "run_cmd", failing_run_cmd)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await tui_module.watch_tmux_window("claude-42")


# ── has_active_claude_session tests ──────────────────────────────────────────


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr("pr_manager.processor.CLAUDE_SESSIONS_DIR", d)
    return d


def test_detects_live_session_in_clone_path(sessions_dir):
    """A session file whose cwd matches the clone path and whose PID is alive
    should be detected."""
    clone = Path("/fake/clone/pr-42")
    session = {
        "pid": os.getpid(),  # current process — guaranteed alive
        "sessionId": "abc-123",
        "cwd": str(clone),
        "startedAt": 1776000000000,
    }
    (sessions_dir / f"{os.getpid()}.json").write_text(json.dumps(session))

    assert has_active_claude_session(clone) is True


def test_ignores_dead_session(sessions_dir):
    """A session file with a non-existent PID should be ignored."""
    clone = Path("/fake/clone/pr-42")
    dead_pid = 2_000_000  # almost certainly not running
    session = {
        "pid": dead_pid,
        "sessionId": "dead-session",
        "cwd": str(clone),
        "startedAt": 1776000000000,
    }
    (sessions_dir / f"{dead_pid}.json").write_text(json.dumps(session))

    assert has_active_claude_session(clone) is False


def test_ignores_session_in_different_directory(sessions_dir):
    """A live session in a different cwd should not count."""
    clone = Path("/fake/clone/pr-42")
    other = Path("/some/other/dir")
    session = {
        "pid": os.getpid(),
        "sessionId": "other-dir",
        "cwd": str(other),
        "startedAt": 1776000000000,
    }
    (sessions_dir / f"{os.getpid()}.json").write_text(json.dumps(session))

    assert has_active_claude_session(clone) is False


def test_no_sessions_dir(tmp_path, monkeypatch):
    """If ~/.claude/sessions/ doesn't exist, return False gracefully."""
    monkeypatch.setattr(
        "pr_manager.processor.CLAUDE_SESSIONS_DIR",
        tmp_path / "nonexistent",
    )
    assert has_active_claude_session(Path("/fake/clone")) is False


def test_malformed_session_file_is_skipped(sessions_dir):
    """Corrupt JSON in a session file should not cause a crash."""
    clone = Path("/fake/clone/pr-42")
    (sessions_dir / "99999.json").write_text("NOT JSON{{{")

    assert has_active_claude_session(clone) is False


def test_session_cwd_is_subdirectory_of_clone(sessions_dir):
    """A session whose cwd is inside the clone path should be detected."""
    clone = Path("/fake/clone/pr-42")
    session = {
        "pid": os.getpid(),
        "sessionId": "sub-dir",
        "cwd": str(clone / "src" / "lib"),
        "startedAt": 1776000000000,
    }
    (sessions_dir / f"{os.getpid()}.json").write_text(json.dumps(session))

    assert has_active_claude_session(clone) is True
