"""Tests for syncing the working clone to origin before agent work.

Regression: the fix loop never synced the local branch to origin/<branch>.
With a reused clone, a human could push commits to the PR; the agent then
rebased the stale local branch, and because every loop iteration fetches
(re-arming the force-with-lease lease), git_push_force_with_lease happily
force-pushed the human's commits out of existence.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pr_manager import fix as fix_module
from pr_manager.git import git_sync_branch_to_origin


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c", "user.email=test@example.com",
            "-c", "user.name=Test",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(cwd: Path, filename: str, content: str, message: str) -> str:
    (cwd / filename).write_text(content)
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def git_remote_and_clone(tmp_path):
    """A bare 'origin' with a feature branch, and a working clone of it."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _commit(seed, "base.txt", "base", "base commit")
    _git(seed, "checkout", "-b", "feature")
    _commit(seed, "feat.txt", "v1", "feature commit")

    origin = tmp_path / "origin.git"
    _git(seed, "clone", "--bare", str(seed), str(origin))
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-b", "feature", str(origin), str(clone))
    return origin, clone


@pytest.mark.asyncio
async def test_sync_adopts_commits_humans_pushed(git_remote_and_clone, tmp_path):
    origin, clone = git_remote_and_clone

    # A human pushes a new commit to the PR branch via their own clone.
    human = tmp_path / "human"
    _git(tmp_path, "clone", "-b", "feature", str(origin), str(human))
    human_sha = _commit(human, "feat.txt", "v2 by human", "human's commit")
    _git(human, "push", "origin", "feature")

    synced = await git_sync_branch_to_origin(clone, "feature")

    assert synced is True
    assert _git(clone, "rev-parse", "HEAD") == human_sha, (
        "local branch must be reset to origin/feature so the human's "
        "commit can't be force-pushed away"
    )


@pytest.mark.asyncio
async def test_sync_discards_stale_local_commits(git_remote_and_clone):
    """Local-only commits (leftovers of a crashed agent run) are discarded —
    origin is the source of truth."""
    origin, clone = git_remote_and_clone
    remote_sha = _git(clone, "rev-parse", "HEAD")
    _commit(clone, "feat.txt", "stale local work", "unpushed leftover")

    synced = await git_sync_branch_to_origin(clone, "feature")

    assert synced is True
    assert _git(clone, "rev-parse", "HEAD") == remote_sha


@pytest.mark.asyncio
async def test_sync_returns_false_when_remote_branch_is_gone(git_remote_and_clone):
    origin, clone = git_remote_and_clone
    _git(clone, "push", "origin", "--delete", "feature")

    synced = await git_sync_branch_to_origin(clone, "feature")

    assert synced is False


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_fix_loop_syncs_before_every_iteration():
    """run_fix must sync the clone to origin before measuring/acting."""
    order: list[str] = []

    def op(name, result=None, stop_at_call=None):
        calls = {"n": 0}

        async def _op(*args, **kwargs):
            calls["n"] += 1
            order.append(name)
            if stop_at_call is not None and calls["n"] >= stop_at_call:
                raise _Stop()
            return result

        return _op

    with (
        patch.object(fix_module, "_fetch_pr_data", AsyncMock(return_value={
            "number": 42, "title": "t", "headRefName": "feature",
            "baseRefName": "main", "state": "OPEN",
        })),
        patch.object(fix_module, "git_update_pristine", AsyncMock()),
        patch.object(fix_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(fix_module, "git_sync_branch_to_origin", op("sync", True)),
        patch.object(fix_module, "git_commits_behind", op("behind", 0, stop_at_call=2)),
        patch.object(fix_module, "gh_pr_check_status", op("checks", ("pending", ""))),
        patch.object(fix_module, "_wait", op("wait")),
    ):
        with pytest.raises(_Stop):
            await fix_module.run_fix("https://github.com/foo/bar/pull/42")

    first_behind = order.index("behind")
    assert "sync" in order[:first_behind], (
        "the clone must be synced to origin before the first loop iteration"
    )
    second_behind = first_behind + 1 + order[first_behind + 1:].index("behind")
    assert "sync" in order[first_behind + 1:second_behind], (
        "every loop iteration must re-sync before acting"
    )
