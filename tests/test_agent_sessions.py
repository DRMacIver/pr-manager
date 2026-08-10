"""Tests for agent session persistence.

Session resume is what lets run_ci_fix_retry continue the SAME agent
conversation after a rejected UNFIXABLE claim; if the session_id is not
saved from the init message or not passed back as resume, retries
silently lose all context.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import SystemMessage

from pr_manager.state import PRState

from .agent_helpers import install_fake_query, make_result_message, make_runner


@pytest.mark.asyncio
async def test_init_message_saves_session_id(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    install_fake_query(monkeypatch, messages=[
        SystemMessage(subtype="init", data={"session_id": "new-sess"}),
        make_result_message("DONE"),
    ])

    await runner._run_agent("do something")

    upserts = runner._state_manager.upsert_pr_state.await_args_list
    assert upserts, "session_id from the init message must be persisted"
    saved_state = upserts[-1].args[2]
    assert saved_state.session_id == "new-sess"


@pytest.mark.asyncio
async def test_saved_session_id_is_resumed(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    runner._state_manager.get_pr_state = AsyncMock(
        return_value=PRState(session_id="sess-9"),
    )
    calls = install_fake_query(monkeypatch)

    await runner._run_agent("continue")

    assert calls[0]["options"].resume == "sess-9"


@pytest.mark.asyncio
async def test_non_persistent_runs_do_not_resume(tmp_path, monkeypatch):
    """The UNFIXABLE reviewer must get a fresh session, not the fix
    agent's history."""
    runner = make_runner(tmp_path)
    runner._state_manager.get_pr_state = AsyncMock(
        return_value=PRState(session_id="sess-9"),
    )
    calls = install_fake_query(monkeypatch)

    await runner._run_agent("review this", persist_session=False)

    assert calls[0]["options"].resume is None
