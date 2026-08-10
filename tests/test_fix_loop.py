"""Tests for the `pr-manager fix` control loop."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_manager import fix as fix_module
from pr_manager.fix import _final_token


# ── agent-result token parsing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "token"),
    [
        ("DONE", "DONE"),
        ("DONE\n", "DONE"),
        ("All rebased cleanly.\n\nDONE", "DONE"),
        ("**DONE**", "DONE"),
        ("UNFIXABLE", "UNFIXABLE"),
        ("These failures are upstream breakage.\nUNFIXABLE", "UNFIXABLE"),
        # Regression: substring matching treated these as success/refusal.
        ("I could NOT get this DONE — the rebase has unresolved conflicts", None),
        ("This is not UNFIXABLE, I fixed it, but tests still run", None),
        ("", None),
        (None, None),
    ],
)
def test_final_token(result, token):
    assert _final_token(result) == token


# ── parse_pr_url ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/foo/bar/pull/123", ("foo/bar", 123)),
        ("http://github.com/a-b/c.d/pull/1", ("a-b/c.d", 1)),
    ],
)
def test_parse_pr_url_valid(url, expected):
    assert fix_module.parse_pr_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/foo/bar",
        "https://gitlab.com/foo/bar/pull/1",
        "not a url",
        "https://github.com/foo/bar/pull/abc",
    ],
)
def test_parse_pr_url_invalid(url):
    with pytest.raises(ValueError):
        fix_module.parse_pr_url(url)


# ── run_fix loop priorities ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_behind_target_rebases_before_looking_at_checks():
    with (
        patch.object(fix_module, "_fetch_pr_data", AsyncMock(return_value=_pr_data())),
        patch.object(fix_module, "git_update_pristine", AsyncMock()),
        patch.object(fix_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(fix_module, "git_sync_branch_to_origin", AsyncMock(return_value=True)),
        patch.object(fix_module, "git_commits_behind", AsyncMock(return_value=3)),
        patch.object(fix_module, "gh_pr_check_status", AsyncMock()) as checks,
        patch.object(fix_module, "_do_rebase", AsyncMock(return_value=True)) as rebase,
        patch.object(fix_module, "_wait", _stop_wait),
    ):
        with pytest.raises(_Stop):
            await fix_module.run_fix("https://github.com/foo/bar/pull/42")

    rebase.assert_awaited_once()
    checks.assert_not_awaited()


@pytest.mark.asyncio
async def test_green_checks_finish_the_run():
    with (
        patch.object(fix_module, "_fetch_pr_data", AsyncMock(return_value=_pr_data())),
        patch.object(fix_module, "git_update_pristine", AsyncMock()),
        patch.object(fix_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(fix_module, "git_sync_branch_to_origin", AsyncMock(return_value=True)),
        patch.object(fix_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(fix_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
        patch.object(fix_module, "_do_ci_fix", AsyncMock()) as fixer,
        patch.object(fix_module, "_wait", _stop_wait),
    ):
        await fix_module.run_fix("https://github.com/foo/bar/pull/42")

    fixer.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_pr_exits_without_touching_git():
    with (
        patch.object(
            fix_module, "_fetch_pr_data",
            AsyncMock(return_value=_pr_data(state="MERGED")),
        ),
        patch.object(fix_module, "git_update_pristine", AsyncMock()) as pristine,
    ):
        with pytest.raises(SystemExit):
            await fix_module.run_fix("https://github.com/foo/bar/pull/42")

    pristine.assert_not_awaited()


# ── _do_rebase ───────────────────────────────────────────────────────────────


def _runner_mock(**results) -> MagicMock:
    runner = MagicMock()
    for name, value in results.items():
        setattr(runner, name, AsyncMock(return_value=value))
    return runner


async def _run_do_rebase(agent_result, push_ok=True):
    state_manager = AsyncMock()
    runner = _runner_mock(run_rebase=agent_result)
    with (
        patch.object(fix_module, "AgentRunner", MagicMock(return_value=runner)),
        patch.object(fix_module, "git_get_current_sha", AsyncMock(return_value="old")),
        patch.object(
            fix_module, "git_push_force_with_lease", AsyncMock(return_value=push_ok)
        ) as push,
        patch.object(
            fix_module, "git_get_new_commits_since", AsyncMock(return_value=["n1"])
        ),
    ):
        ok = await fix_module._do_rebase(
            "foo/bar", 42, "feat", "/clone", "/log", "main", state_manager,
        )
    return ok, push, state_manager


@pytest.mark.asyncio
async def test_rebase_success_pushes_and_records():
    ok, push, sm = await _run_do_rebase("Rebased.\nDONE")
    assert ok is True
    push.assert_awaited_once()
    sm.record_our_commits.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebase_failure_narration_containing_done_does_not_push():
    """Regression: 'could NOT get this DONE' must not be treated as success
    and force-pushed."""
    ok, push, sm = await _run_do_rebase(
        "I could NOT get this DONE — unresolved conflicts remain."
    )
    assert ok is False
    push.assert_not_awaited()
    sm.record_our_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebase_rejected_push_is_failure_and_not_recorded():
    ok, push, sm = await _run_do_rebase("DONE", push_ok=False)
    assert ok is False
    sm.record_our_commits.assert_not_awaited()


# ── _do_ci_fix ───────────────────────────────────────────────────────────────


async def _run_do_ci_fix(
    fix_result,
    review=("accept", "fine"),
    retry_result=None,
    sha_after="new",
    push_ok=True,
):
    state_manager = AsyncMock()
    runner = MagicMock()
    runner.run_ci_fix = AsyncMock(return_value=fix_result)
    runner.run_ci_fix_review = AsyncMock(return_value=review)
    runner.run_ci_fix_retry = AsyncMock(return_value=retry_result)
    shas = iter(["old", sha_after])

    async def fake_sha(_path):
        return next(shas)

    with (
        patch.object(fix_module, "AgentRunner", MagicMock(return_value=runner)),
        patch.object(fix_module, "git_get_current_sha", fake_sha),
        patch.object(
            fix_module, "git_push_force_with_lease", AsyncMock(return_value=push_ok)
        ) as push,
        patch.object(
            fix_module, "git_get_new_commits_since", AsyncMock(return_value=["n1"])
        ),
    ):
        ok = await fix_module._do_ci_fix(
            "foo/bar", 42, "feat", "/clone", "/log", "failures", state_manager, "t",
        )
    return ok, push, state_manager, runner


@pytest.mark.asyncio
async def test_ci_fix_done_with_changes_pushes():
    ok, push, sm, _runner = await _run_do_ci_fix("Fixed it.\nDONE")
    assert ok is True
    push.assert_awaited_once()
    sm.record_our_commits.assert_awaited_once()


@pytest.mark.asyncio
async def test_ci_fix_done_without_changes_does_not_push():
    ok, push, _sm, _runner = await _run_do_ci_fix("DONE", sha_after="old")
    assert ok is True
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_ci_fix_rejected_push_is_failure_and_not_recorded():
    ok, _push, sm, _runner = await _run_do_ci_fix("DONE", push_ok=False)
    assert ok is False
    sm.record_our_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_ci_fix_unfixable_accepted_review_gives_up():
    ok, push, _sm, runner = await _run_do_ci_fix(
        "UNFIXABLE", review=("accept", "genuinely unrelated"),
    )
    assert ok is False
    push.assert_not_awaited()
    runner.run_ci_fix_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_ci_fix_unfixable_rejected_review_retries_and_pushes():
    ok, push, _sm, runner = await _run_do_ci_fix(
        "UNFIXABLE", review=("reject", "you must fix this"), retry_result="DONE",
    )
    assert ok is True
    runner.run_ci_fix_retry.assert_awaited_once()
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_ci_fix_unfixable_twice_gives_up():
    ok, push, _sm, _runner = await _run_do_ci_fix(
        "UNFIXABLE", review=("reject", "fix it"), retry_result="Still UNFIXABLE\nUNFIXABLE",
    )
    assert ok is False
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_ci_fix_none_result_is_failure():
    ok, push, _sm, _runner = await _run_do_ci_fix(None)
    assert ok is False
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_ci_fix_mentioning_unfixable_midtext_is_not_a_refusal():
    """Regression: an agent that fixed the problem but mentioned the word
    UNFIXABLE mid-narration was diverted into the refusal/review path."""
    ok, _push, _sm, runner = await _run_do_ci_fix(
        "At first this looked UNFIXABLE, but the flake was in this PR.\nDONE"
    )
    assert ok is True
    runner.run_ci_fix_review.assert_not_awaited()


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
