"""Tests for how the TUI app is launched.

Regression: commit bc5a2de tried to disable Textual's mouse capture (so
terminal text selection works) by setting `ENABLE_MOUSE_SUPPORT = False`
on the App subclass — but no such attribute exists in Textual; it was
inert, and mouse capture stayed on. The supported mechanism is
`App.run(mouse=False)`.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pr_manager import _main


def test_run_launches_app_without_mouse_capture(monkeypatch):
    monkeypatch.setenv("TMUX", "fake-tmux-socket")
    monkeypatch.setattr("sys.argv", ["pr-manager", "run"])

    app = MagicMock()
    app.return_code = 0
    app_cls = MagicMock(return_value=app)

    with (
        patch("pr_manager.claude_auth.ensure_logged_in"),
        patch("pr_manager.tui.PRManagerApp", app_cls),
    ):
        _main()

    app.run.assert_called_once_with(mouse=False)


def test_tui_module_has_no_inert_mouse_attribute():
    """ENABLE_MOUSE_SUPPORT is not a Textual API; keeping it around
    suggests mouse capture is handled when it isn't."""
    from pr_manager.tui import PRManagerApp

    assert not hasattr(PRManagerApp, "ENABLE_MOUSE_SUPPORT")
