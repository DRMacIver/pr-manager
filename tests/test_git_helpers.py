"""Table tests pinning git.py helper behavior.

These helpers gate commit rewriting and force-pushing; nothing else in
the suite pins their exact semantics, so a well-meaning refactor could
flip them silently.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pr_manager.git import git_latest_commit_is_bot, git_reattribute_and_push


# ── bot-author detection ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "is_bot"),
    [
        ("dependabot[bot]@users.noreply.github.com", True),
        ("49699333+dependabot[bot]@users.noreply.github.com", True),
        ("claude[bot]@example.com", True),          # [bot] outside noreply still counts
        ("human@example.com", False),
        ("12345+someuser@users.noreply.github.com", False),  # noreply but not a bot
        ("some-bot@users.noreply.github.com", True),  # noreply + 'bot' in name
        ("bot@example.com", False),                   # 'bot' alone isn't enough off noreply
    ],
)
async def test_bot_email_detection(email, is_bot):
    with patch(
        "pr_manager.git.run_cmd", AsyncMock(return_value=(0, email, "")),
    ):
        assert await git_latest_commit_is_bot("foo/bar", "feat") is is_bot


@pytest.mark.asyncio
async def test_bot_detection_defaults_to_human_on_gh_failure():
    """If we can't tell, assume human — never rewrite a commit on a guess."""
    with patch(
        "pr_manager.git.run_cmd", AsyncMock(return_value=(1, "", "boom")),
    ):
        assert await git_latest_commit_is_bot("foo/bar", "feat") is False


# ── reattribution sequencing ─────────────────────────────────────────────────


async def _run_reattribute(fail_on: str | None = None):
    calls: list[list[str]] = []

    async def fake_run_cmd(args, cwd=None, check=True, timeout=None):
        calls.append(list(args))
        if fail_on is not None and fail_on in args:
            return 1, "", "failed"
        return 0, "", ""

    with patch("pr_manager.git.run_cmd", fake_run_cmd):
        ok = await git_reattribute_and_push(Path("/clone"), "feat")
    return ok, calls


@pytest.mark.asyncio
async def test_reattribute_runs_reset_amend_push_in_order():
    ok, calls = await _run_reattribute()
    assert ok is True
    steps = [c[1] for c in calls]
    assert steps == ["reset", "commit", "push"]
    assert "--reset-author" in calls[1]
    assert "--force-with-lease" in calls[2]


@pytest.mark.asyncio
async def test_reattribute_stops_before_push_if_amend_fails():
    """A failed amend must not be followed by a force-push of whatever
    state the clone happens to be in."""
    ok, calls = await _run_reattribute(fail_on="--amend")
    assert ok is False
    assert not any(c[1] == "push" for c in calls)


@pytest.mark.asyncio
async def test_reattribute_stops_if_reset_fails():
    ok, calls = await _run_reattribute(fail_on="reset")
    assert ok is False
    assert [c[1] for c in calls] == ["reset"]
