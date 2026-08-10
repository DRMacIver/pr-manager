"""Shared helpers for AgentRunner unit tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from claude_agent_sdk import ResultMessage

from pr_manager import agent as agent_module
from pr_manager.agent import AgentRunner


def make_runner(tmp_path) -> AgentRunner:
    return AgentRunner(
        repo="foo/bar",
        pr_number=1,
        branch="some-branch",
        worktree_path=tmp_path,
        state_manager=MagicMock(
            get_pr_state=AsyncMock(return_value=None),
            upsert_pr_state=AsyncMock(),
        ),
        log_path=tmp_path / "agent.log",
    )


def make_result_message(result: str | None = None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="test-session",
        result=result,
    )


def install_fake_query(monkeypatch, messages=()):
    """Replace pr_manager.agent.query with a stub yielding `messages`.

    Returns a list that captures the (prompt, options) of each call.
    """
    calls = []

    async def fake_query(*, prompt, options):
        calls.append({"prompt": prompt, "options": options})
        for message in messages:
            yield message

    monkeypatch.setattr(agent_module, "query", fake_query)
    return calls
