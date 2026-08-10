"""Regression tests: agents must run via the system `claude` CLI.

The claude-agent-sdk prefers a CLI binary bundled inside the Python
package over the `claude` on PATH.  That bundled copy goes stale — e.g.
SDK 0.1.54 bundles Claude Code 2.1.90, which predates the `fable` model
alias — so agents spawned by pr-manager failed with "There's an issue
with the selected model (fable)" even though the system CLI handled the
same settings fine.  Passing `cli_path` pins the SDK to the system
installation so it can't drift behind it.
"""
from __future__ import annotations

import shutil

import pytest

from .agent_helpers import install_fake_query, make_runner


@pytest.mark.asyncio
async def test_run_agent_uses_system_claude_cli(tmp_path, monkeypatch):
    """The SDK must be pointed at the `claude` on PATH, not its bundled CLI."""
    monkeypatch.setattr(shutil, "which", lambda cmd: {"claude": "/fake/bin/claude"}.get(cmd))
    calls = install_fake_query(monkeypatch)
    await make_runner(tmp_path)._run_agent("do something", persist_session=False)
    assert calls[0]["options"].cli_path == "/fake/bin/claude"


@pytest.mark.asyncio
async def test_run_agent_falls_back_to_sdk_discovery_without_system_cli(tmp_path, monkeypatch):
    """With no `claude` on PATH, leave cli_path unset so the SDK's own
    discovery (including its bundled CLI) still works."""
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    calls = install_fake_query(monkeypatch)
    await make_runner(tmp_path)._run_agent("do something", persist_session=False)
    assert calls[0]["options"].cli_path is None
