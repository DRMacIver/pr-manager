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


def test_ctrl_c_exits_instead_of_trapping_into_press_q():
    """Regression: main()'s `except BaseException` handler caught
    KeyboardInterrupt and then blocked reading stdin for 'q' — so a user
    who aborted startup (e.g. an abandoned login wait) got stuck in a
    second trap instead of their Ctrl-C working."""
    import pytest

    from pr_manager import main

    with (
        patch("pr_manager._main", side_effect=KeyboardInterrupt),
        patch("sys.stdin") as stdin,
    ):
        with pytest.raises(SystemExit) as excinfo:
            main()

    assert excinfo.value.code == 130
    stdin.read.assert_not_called()


def test_login_wait_times_out_instead_of_hanging_forever(monkeypatch, capsys):
    """Regression: `while not is_logged_in(): time.sleep(1)` never gave
    up — closing the claude-login window without authenticating left
    pr-manager waiting forever."""
    import pytest

    from pr_manager import claude_auth

    monkeypatch.setattr(claude_auth.platform, "system", lambda: "Linux")
    monkeypatch.setenv("TMUX", "fake")
    monkeypatch.setattr(claude_auth, "is_logged_in", lambda: False)
    monkeypatch.setattr(claude_auth.subprocess, "run", MagicMock())
    monkeypatch.setattr(claude_auth, "_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(SystemExit):
        claude_auth.ensure_logged_in(timeout_seconds=0.05)

    assert "log" in capsys.readouterr().err.lower()
