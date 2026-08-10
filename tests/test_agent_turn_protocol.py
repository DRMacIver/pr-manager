"""Regression tests: fix agents must not end their turn waiting to be woken.

pr-manager drives each agent through a single SDK query(); the query is
over when the agent ends its turn.  Claude Code >= 2.1.x gives agents
background Bash tasks and a ScheduleWakeup tool, and an agent that started
a slow check in the background, scheduled a wakeup, and ended its turn
produced a final result without DONE — the wakeup never fires under
query(), so pr-manager declared "CI fix agent did not complete".

Also covers cleanup: breaking out of the query() generator without closing
it left it suspended, and interpreter shutdown then failed with
"aclose(): asynchronous generator is already running".
"""
from __future__ import annotations

import pytest

from pr_manager import agent as agent_module

from .agent_helpers import install_fake_query, make_result_message, make_runner


@pytest.mark.asyncio
async def test_schedule_wakeup_is_disallowed(tmp_path, monkeypatch):
    """The agent must not be able to schedule wakeups: nothing re-invokes it."""
    calls = install_fake_query(monkeypatch)
    await make_runner(tmp_path)._run_agent("do something", persist_session=False)
    assert "ScheduleWakeup" in calls[0]["options"].disallowed_tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda runner: runner.run_rebase("main"),
        lambda runner: runner.run_ci_fix("some CI failures"),
        lambda runner: runner.run_ci_fix_retry("reviewer feedback"),
        lambda runner: runner.run_ci_fix_review("UNFIXABLE", "failures", "title"),
    ],
)
async def test_prompts_require_foreground_execution(tmp_path, monkeypatch, call):
    """Every agent prompt must say to wait in the foreground rather than
    ending the turn with background work pending."""
    calls = install_fake_query(monkeypatch)
    await call(make_runner(tmp_path))
    prompt = calls[0]["prompt"]
    assert "run_in_background" in prompt
    assert "foreground" in prompt
    assert "never end your turn" in prompt


@pytest.mark.asyncio
async def test_query_generator_closed_before_run_agent_returns(tmp_path, monkeypatch):
    """Breaking out of query() at the ResultMessage must still close the
    generator, not leave it suspended until interpreter shutdown."""
    state = {"closed": False}

    async def fake_query(*, prompt, options):
        try:
            yield make_result_message("DONE")
            yield make_result_message("NEVER REACHED")
        finally:
            state["closed"] = True

    monkeypatch.setattr(agent_module, "query", fake_query)
    result = await make_runner(tmp_path)._run_agent("do something", persist_session=False)
    assert result == "DONE"
    assert state["closed"], "query() generator was left open after _run_agent returned"
