"""Shared helpers for TUI tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pr_manager.state import Settings
from pr_manager.tui import PRManagerApp


def mock_state_manager() -> MagicMock:
    """A StateManager double sufficient to mount PRManagerApp."""
    sm = MagicMock()
    sm.get_settings = AsyncMock(return_value=Settings())
    sm.get_repos = AsyncMock(return_value=[])
    sm.get_all_pr_states = AsyncMock(return_value={})
    sm.get_local_branches = AsyncMock(return_value=[])
    return sm


def mk_app() -> PRManagerApp:
    return PRManagerApp(mock_state_manager(), poll_interval=5)
