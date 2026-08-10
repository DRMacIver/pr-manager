"""Tests for the `pr-manager fix` control loop."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pr_manager import fix as fix_module


class _Stop(Exception):
    pass


async def _stop_wait(seconds: float) -> None:
    raise _Stop()


def _pr_data(**overrides) -> dict:
    data = {
        "number": 42,
        "title": "a pr",
        "headRefName": "feat",
        "baseRefName": "main",
        "body": "",
        "isDraft": False,
        "state": "OPEN",
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_error_check_status_waits_instead_of_fixing():
    """A gh failure (auth, network) must not be treated as failing CI —
    no fix agent may run; the loop logs and retries later."""
    with (
        patch.object(fix_module, "_fetch_pr_data", AsyncMock(return_value=_pr_data())),
        patch.object(fix_module, "git_update_pristine", AsyncMock()),
        patch.object(fix_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(fix_module, "git_sync_branch_to_origin", AsyncMock(return_value=True)),
        patch.object(fix_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(
            fix_module,
            "gh_pr_check_status",
            AsyncMock(return_value=("error", "HTTP 401: Bad credentials")),
        ),
        patch.object(fix_module, "_do_ci_fix", AsyncMock()) as mock_fix,
        patch.object(fix_module, "_do_rebase", AsyncMock()) as mock_rebase,
        patch.object(fix_module, "_wait", _stop_wait),
    ):
        with pytest.raises(_Stop):
            await fix_module.run_fix("https://github.com/foo/bar/pull/42")

    mock_fix.assert_not_called()
    mock_rebase.assert_not_called()
