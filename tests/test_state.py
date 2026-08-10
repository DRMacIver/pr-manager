"""Tests for StateManager persistence.

Two properties that were previously unenforced:

1. Cross-process safety: `pr-manager run` (TUI) and `pr-manager fix`
   each hold their own StateManager over the same state.json. Each
   used to load once at startup and rewrite the whole file from its
   in-memory snapshot on every save — so whichever process saved last
   silently erased the other's writes (lost session_ids/our_commits).
   Now every operation is a fresh read-modify-write under a file lock.

2. Corruption handling: a truncated state.json used to crash every
   command at startup. Now the corrupt file is set aside and we start
   fresh, loudly.
"""
from __future__ import annotations

import json

import pytest

from pr_manager.state import PRState, Settings, StateManager


async def _manager() -> StateManager:
    sm = StateManager()
    await sm.load()
    return sm


# ── round-trip ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_round_trip(state_path):
    """Everything written through the API must survive a save/load cycle
    through a fresh StateManager (catches _save_sync/load field drift)."""
    a = await _manager()
    await a.add_repo("foo/bar")
    await a.add_local_branch("foo/bar", "wip-branch")
    pr = PRState(
        session_id="sess-1",
        our_commits=["abc123"],
        status="green",
        title="My PR",
        branch="feat",
        created_at="2026-01-01T00:00:00Z",
        is_draft=True,
        review_decision="APPROVED",
        comment_count=3,
        review_count=1,
        latest_activity="2026-02-01T00:00:00Z",
    )
    await a.upsert_pr_state("foo/bar", "7", pr)
    await a.update_settings(Settings(claude_permission_mode="acceptEdits"))

    b = await _manager()
    assert await b.get_repos() == ["foo/bar"]
    assert await b.get_local_branches("foo/bar") == ["wip-branch"]
    assert await b.get_pr_state("foo/bar", "7") == pr
    assert (await b.get_settings()).claude_permission_mode == "acceptEdits"


# ── cross-process safety ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_managers_do_not_clobber_each_other(state_path):
    """Writes from two managers (≈ two processes) must both survive."""
    tui = await _manager()
    fixproc = await _manager()

    # The fix process records a session id…
    await fixproc.upsert_pr_state(
        "foo/bar", "42", PRState(session_id="sess-42", title="t", branch="b"),
    )
    # …then the TUI (which loaded before that) writes something else.
    await tui.add_repo("foo/bar")

    fresh = await _manager()
    assert await fresh.get_repos() == ["foo/bar"], "TUI write lost"
    pr = await fresh.get_pr_state("foo/bar", "42")
    assert pr is not None and pr.session_id == "sess-42", (
        "fix process's write was clobbered by the TUI's stale snapshot"
    )


@pytest.mark.asyncio
async def test_reads_see_other_processes_writes(state_path):
    """A manager must observe state another manager wrote after it loaded."""
    tui = await _manager()
    fixproc = await _manager()

    await fixproc.record_our_commits("foo/bar", "42", ["abc"])

    pr = await tui.get_pr_state("foo/bar", "42")
    assert pr is not None and pr.our_commits == ["abc"]


@pytest.mark.asyncio
async def test_record_our_commits_unions(state_path):
    sm = await _manager()
    await sm.record_our_commits("foo/bar", "42", ["a", "b"])
    await sm.record_our_commits("foo/bar", "42", ["b", "c"])
    pr = await sm.get_pr_state("foo/bar", "42")
    assert pr is not None
    assert sorted(pr.our_commits) == ["a", "b", "c"]


# ── corruption handling ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corrupt_state_file_is_set_aside_not_fatal(state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"repos": ["foo/bar"], "pr_st')  # truncated

    sm = await _manager()

    assert await sm.get_repos() == []
    backups = list(state_path.parent.glob("state.json.corrupt-*"))
    assert backups, "the corrupt file must be preserved for inspection"
    assert not state_path.exists() or json.loads(state_path.read_text() or "{}") is not None

    # And the manager must be fully usable afterwards.
    await sm.add_repo("foo/bar")
    assert await sm.get_repos() == ["foo/bar"]


# ── forward compatibility ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_top_level_and_pr_keys_are_ignored(state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "repos": ["foo/bar"],
        "disabled_prs": {"foo/bar": ["1"]},          # legacy key
        "pr_state": {
            "foo/bar": {"7": {"title": "t", "branch": "b", "stacked_on": "x"}},
        },
        "local_branches": {},
        "settings": {"claude_permission_mode": "acceptEdits", "future_key": 1},
    }))

    sm = await _manager()
    pr = await sm.get_pr_state("foo/bar", "7")
    assert pr is not None and pr.title == "t"
    assert (await sm.get_settings()).claude_permission_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_invalid_settings_fall_back_to_defaults(state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "repos": [],
        "pr_state": {},
        "local_branches": {},
        "settings": {"claude_permission_mode": "yolo", "theme": "no-such-theme"},
    }))

    sm = await _manager()
    settings = await sm.get_settings()
    assert settings.claude_permission_mode == "default"
    assert settings.theme == Settings.theme
