# Explicit PR-fix command — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the poll loop's auto-fix decision logic with an explicit user-invoked fix command, invokable via CLI (`pr-manager fix <url>`, already exists) or a new TUI `f` binding that opens the CLI in a tmux window.

**Architecture:** Poll loop becomes status-only (gh + git reads, writes to `PRState.status` for display only). `PRProcessor` and all of its decision logic (stacked PRs, human-changes guard, active-Claude-session guard, bot-commit reattribution, auto-rebase, auto-CI-fix) are deleted. Fix-session progress is shown in the TUI via an overlay driven by a `watch_tmux_window` sentinel in `_active_tasks`.

**Tech Stack:** Python 3, asyncio, Textual, `gh` CLI, `git`, `tmux`, Claude Agent SDK (unchanged).

---

## File Structure

Files created:
- None.

Files modified:
- `pr_manager/state.py` — drop `stacked_on`, `disabled_prs`, related methods.
- `pr_manager/git.py` — add `baseRefName` to `gh_list_prs`; delete `gh_get_recent_commits` and `git_is_ancestor`.
- `pr_manager/poll.py` — rewrite as status-only; add `compute_pr_status` helper.
- `pr_manager/constants.py` — update `STATUS_STYLE` for new status set.
- `pr_manager/display.py` — `is_active` no longer read from `PRState.status`.
- `pr_manager/tui.py` — add `f` binding + `action_fix`; simplify `action_toggle_disabled`; overlay "fixing" status from sentinel.
- `pr_manager/headless.py` — nothing structural (inherits status changes via constants).
- `pr_manager/assistant_api.py` — drop `disable_pr`/`enable_pr`; `list_running_agents` semantics still hold (sentinels instead of processors).
- `README.md` — update automatic-action description and keybinding table.

Files deleted:
- `pr_manager/processor.py`
- `tests/test_disable_pr.py`
- `tests/test_user_session_guard.py`

Tests modified/replaced:
- `tests/test_cleanup_safety.py` — stop patching `PRProcessor`; verify status-only poll still preserves clones.
- `tests/test_poll_nudge.py` — stop patching `PRProcessor`; verify nudge semantics still apply (see note in Task 6).
- `tests/test_branch_adoption.py` — remove stale docstring reference to `has_active_claude_session`.

---

## Preconditions

Run all existing tests and confirm they pass before starting:

```bash
cd /workspace && uv run pytest
```

If any already fail, stop and fix them first — we need a green baseline.

---

## Task 1: Remove the PR disable/enable feature

The poll loop no longer performs writes, so "disabled" stops having meaning. Remove the whole end-to-end feature in one atomic task so no code references the deleted helpers after commit.

**Files:**
- Delete: `tests/test_disable_pr.py`
- Modify: `pr_manager/state.py:27-216` (remove `disabled_prs` field, save serialization, and the four disable/enable methods — but keep the legacy read in `load()`).
- Modify: `pr_manager/assistant_api.py:134-145` (remove `disable_pr` / `enable_pr`).
- Modify: `pr_manager/poll.py:54-61,99-100` (remove `disabled_prs` filtering and enabling-dropped PRs).
- Modify: `pr_manager/tui.py:713-736` (simplify `action_toggle_disabled`: keep local-branch-removal branch, drop PR disable/enable branches).
- Modify: `pr_manager/constants.py:11-22` (remove `disabled` status entry).

- [ ] **Step 1: Delete the disable-pr test module**

```bash
git rm tests/test_disable_pr.py
```

- [ ] **Step 2: Remove `disabled_prs` from `AppState` and state IO**

In `pr_manager/state.py`:

- Remove this line from `AppState` (line 55):
  ```python
      disabled_prs: dict[str, list[int]] = field(default_factory=dict)
  ```
- In `load()`, simplify the `AppState` construction so it no longer populates a `disabled_prs=` kwarg; keep reading both `disabled_prs` and `hidden_prs` keys but discard them (for back-compat with existing state files):
  ```python
          async with self._lock:
              if STATE_PATH.exists():
                  data = json.loads(STATE_PATH.read_text())
                  # Legacy `disabled_prs` / `hidden_prs` keys are ignored
                  # silently for forward-compat with old state files.
                  self._state = AppState(
                      repos=data.get("repos", []),
                      pr_state=data.get("pr_state", {}),
                      local_branches=data.get("local_branches", {}),
                      settings=_dict_to_settings(data.get("settings", {})),
                  )
              else:
                  self._state = AppState()
  ```
- In `_save_sync()`, remove the `"disabled_prs": ...` line from the JSON dict.
- Delete the four methods `disable_pr`, `enable_pr`, `is_disabled`, `get_disabled_prs` entirely (lines 189-213).

- [ ] **Step 3: Remove `disable_pr` / `enable_pr` from `assistant_api.py`**

Delete the two methods at `pr_manager/assistant_api.py:134-145`. Nothing else references them.

- [ ] **Step 4: Simplify `action_toggle_disabled` in `tui.py`**

Replace the whole `action_toggle_disabled` method (currently at lines 713-736) with:

```python
    async def action_toggle_disabled(self) -> None:
        """`x` binding: remove a local branch from the list.

        Has no effect on rows for real PRs — auto-fix is explicit now.
        """
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        if pr.number != 0:
            self.post_message(AppLogMessage(
                "`x` only removes local branches; use `f` to fix a PR", "info",
            ))
            return
        await self._state_manager.remove_local_branch(pr.repo, pr.branch)
        self._display_prs = [
            p for p in self._display_prs
            if not (p.repo == pr.repo and p.number == pr.number and p.branch == pr.branch)
        ]
        self._refresh_table()
        self.post_message(AppLogMessage(
            f"Removed local branch {pr.branch} ({pr.repo}) from list", "info",
        ))
```

- [ ] **Step 5: Remove `disabled` status from `STATUS_STYLE`**

In `pr_manager/constants.py`, remove the `"disabled"` entry (line 20) from `STATUS_STYLE`. (Other status keys stay; they are cleaned up in Task 4.)

- [ ] **Step 6: Remove disabled filtering from `poll.py`**

In `pr_manager/poll.py`, delete these blocks:

- Lines 54-59 (drop disabled-list entries for PRs no longer on GitHub).
- Line 61 (the `disabled = set(...)` read).
- Lines 99-100 (the `if pr_number in disabled: continue`).

The poll loop still has a `PRProcessor` invocation at this point — leave it; it gets deleted in Task 5.

- [ ] **Step 7: Run the test suite**

```bash
cd /workspace && uv run pytest
```

Expected: all tests pass (those that remain). If `test_cleanup_safety.py::test_disabled_pr_clone_not_deleted_when_still_on_github` fails because it calls `sm.disable_pr`, delete just that single test function from that file — the broader cleanup test (`test_cleanup_does_not_remove_clone_for_pr_missing_from_gh_list`) still covers the important guard and remains valid.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Remove PR disable/enable feature

Auto-fix is becoming explicit, so there is no longer anything to
disable. The `x` binding now only removes local branches from the
list. Existing state files containing `disabled_prs` / `hidden_prs`
keys load silently; the keys are dropped on the next save.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Delete the stacked-PR / human-changes / Claude-session guards

These existed only to gate auto-fix decisions, which are going away. Delete them, together with their tests and the unused git helpers they called.

**Files:**
- Delete: `tests/test_user_session_guard.py`
- Modify: `pr_manager/state.py:13-27` (drop `stacked_on` from `PRState`).
- Modify: `pr_manager/processor.py` (remove stacked-PR, human-changes, and session guards; the whole file is deleted in Task 5 — for this task just strip those sections so the intermediate state still runs).
- Modify: `tests/test_branch_adoption.py:8` (remove stale `has_active_claude_session` mention from the docstring).

- [ ] **Step 1: Delete the session-guard test module**

```bash
git rm tests/test_user_session_guard.py
```

- [ ] **Step 2: Fix docstring in `tests/test_branch_adoption.py`**

Edit the module docstring at `tests/test_branch_adoption.py:1-9`. Replace the existing block with:

```python
"""Tests for local branch → PR adoption.

When a local branch gets a PR created for it, git_setup_pr_clone should
detect the existing branch clone and symlink to it rather than creating
a fresh clone.  This way:
- Active processes in the branch clone are unaffected
- The PR clone path reuses the branch clone's working state
"""
```

- [ ] **Step 3: Drop `stacked_on` from `PRState`**

In `pr_manager/state.py`, remove the `stacked_on` field (line 27) from the `PRState` dataclass. `_dict_to_pr_state` already filters unknown keys via `_PR_STATE_FIELDS`, so old state files still load cleanly.

- [ ] **Step 4: Strip stacked / guards logic from `processor.py`**

In `pr_manager/processor.py`:

- Delete `has_active_claude_session` (lines 28-69) and the `CLAUDE_SESSIONS_DIR` constant.
- Delete `_parse_pr_references` (lines 72-85).
- Delete `_detect_stack` method (lines 263-287).
- Delete `_has_human_changes` method (lines 289-297).
- In `PRProcessor.process()`, remove:
  - The `# 0.` and `# 0b.` blocks (stacked-PR detection and parent-blocking, lines 136-162).
  - The `# 1.` block (human-changes skip, lines 164-170).
  - The `# 1b.` block (active-session skip, lines 172-181).
  - Change the fallback `rebase_target = "main"` so it just uses `"main"` unconditionally (i.e. use the variable name or inline `"main"` directly).
- Remove the `gh_get_recent_commits` and `git_is_ancestor` imports from the top of the file.
- Remove the `json`, `os`, `re` imports if they are no longer referenced after the deletions.

- [ ] **Step 5: Run the tests**

```bash
cd /workspace && uv run pytest
```

Expected: all remaining tests pass. `test_cleanup_safety.py` and `test_poll_nudge.py` still patch `poll_module.PRProcessor` (which still exists in reduced form) — they should continue to pass.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Drop stacked-PR, human-changes, and Claude-session guards

These only existed to gate auto-fix decisions, which are being
replaced with an explicit user-invoked command. `PRState.stacked_on`
disappears from the dataclass; old state files that still have the
field load cleanly because unknown keys are filtered out on decode.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add `baseRefName` to `gh_list_prs`

The new poll loop needs each PR's actual base branch to compute how far behind it is. GitHub already tracks this; we just need to request the field.

**Files:**
- Modify: `pr_manager/git.py:63-69`.

- [ ] **Step 1: Add `baseRefName` to the requested JSON fields**

In `pr_manager/git.py`, the `gh_list_prs` function currently requests:

```python
        "--json", "number,title,headRefName,headRefOid,createdAt,isDraft,reviewDecision,comments,reviews,body",
```

Change to:

```python
        "--json", "number,title,headRefName,baseRefName,headRefOid,createdAt,isDraft,reviewDecision,comments,reviews,body",
```

- [ ] **Step 2: Run the tests**

```bash
cd /workspace && uv run pytest
```

Expected: all pass; the change is additive.

- [ ] **Step 3: Commit**

```bash
git add pr_manager/git.py
git commit -m "$(cat <<'EOF'
Include baseRefName in gh_list_prs response

The new status-only poll loop needs each PR's actual base branch to
compute how many commits behind it is for display.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Update `STATUS_STYLE` for the new status set

**Files:**
- Modify: `pr_manager/constants.py:11-22`.

- [ ] **Step 1: Replace the `STATUS_STYLE` table**

Replace the `STATUS_STYLE` dict in `pr_manager/constants.py` with:

```python
# (icon, rich style) per status
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "idle":      ("○", "dim"),
    "pending":   ("◌", "cyan"),
    "behind":    ("↓", "yellow"),
    "no_checks": ("·", "dim"),
    "failing":   ("✗", "red bold"),
    "fixing":    ("◉", "yellow"),
    "green":     ("✓", "green"),
    "error":     ("!", "red bold"),
    "local":     ("◇", "magenta"),
}
```

The `error` status stays because TUI / state still surface exception strings via `error_message`. `rebasing`, `fixing_ci`, `blocked`, `human_changes`, `disabled` are gone — the single `fixing` overlay replaces the first two, and the last three no longer exist.

- [ ] **Step 2: Run the tests**

```bash
cd /workspace && uv run pytest
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add pr_manager/constants.py
git commit -m "$(cat <<'EOF'
Rework STATUS_STYLE for the status-only poll loop

The observed-status set shrinks to green/pending/failing/behind/
no_checks/error, plus the `fixing` overlay the TUI drapes over a row
while a fix session is running.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Rewrite `poll.py` as status-only; delete `processor.py`

The big swap. The new `poll.py` never writes to GitHub or to the repo; it reads check status, computes behind-count, and updates `PRState.status` so the display can render it. `PRProcessor` is deleted in full. Tests that patched `poll_module.PRProcessor` get rewritten to match.

**Files:**
- Modify: `pr_manager/poll.py` (whole-file rewrite).
- Delete: `pr_manager/processor.py`.
- Modify: `tests/test_cleanup_safety.py` (drop `PRProcessor` patches; the status-only poll keeps the guard).
- Modify: `tests/test_poll_nudge.py` (drop `PRProcessor` patches; verify nudge still wakes the loop).

- [ ] **Step 1: Write a failing test for `compute_pr_status`**

Create a new test file `tests/test_poll_status.py` with:

```python
"""Tests for the status-only poll helper."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pr_manager import poll as poll_module


@pytest.mark.asyncio
async def test_compute_pr_status_behind_takes_priority_over_checks():
    """A PR that is behind base is reported as `behind` even if checks pass."""
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=2)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "behind"


@pytest.mark.asyncio
async def test_compute_pr_status_maps_gh_green():
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "green"


@pytest.mark.asyncio
async def test_compute_pr_status_maps_gh_failing():
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("failing", "x"))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "failing"


@pytest.mark.asyncio
async def test_compute_pr_status_maps_no_checks():
    pr_data = {"number": 1, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("no_checks", ""))),
    ):
        status = await poll_module.compute_pr_status("foo/bar", pr_data, Path("/tmp/fake"))
    assert status == "no_checks"
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
cd /workspace && uv run pytest tests/test_poll_status.py -v
```

Expected: `AttributeError: module 'pr_manager.poll' has no attribute 'compute_pr_status'`.

- [ ] **Step 3: Rewrite `poll.py`**

Replace the entire contents of `pr_manager/poll.py` with:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Protocol

from .display import build_display_list
from .git import (
    gh_list_prs,
    gh_pr_check_status,
    get_clone_path,
    git_commits_behind,
    git_setup_pr_clone,
    git_update_pristine,
    remove_clone,
)
from .state import PRDisplayInfo, PRState, StateManager


class PollHost(Protocol):
    _active_tasks: dict[tuple[str, int], asyncio.Task]

    def on_log(self, text: str, level: str) -> None: ...
    def on_status_update(self, repo: str, pr_number: int, status: str, error: Optional[str]) -> None: ...
    def on_pr_list(self, prs: list[PRDisplayInfo]) -> None: ...


async def compute_pr_status(repo: str, pr_data: dict, clone_path: Path) -> str:
    """Read-only status derivation for a single PR.

    Never writes to GitHub or to the working tree beyond the `git fetch`
    embedded in `git_commits_behind`.
    """
    branch = pr_data["headRefName"]
    base = pr_data.get("baseRefName") or "main"
    behind = await git_commits_behind(clone_path, branch, base)
    if behind > 0:
        return "behind"
    check_status, _details = await gh_pr_check_status(repo, int(pr_data["number"]))
    # gh_pr_check_status returns: green | pending | failing | no_checks
    return check_status


async def poll_loop(
    host: PollHost,
    state_manager: StateManager,
    poll_interval_minutes: int,
    recent_minutes: int,
    nudge: Optional[asyncio.Event] = None,
) -> None:
    """Status-only poll loop. Never writes to PRs.

    `recent_minutes` is accepted for signature compatibility with the
    previous auto-fix loop; it is unused.
    """
    del recent_minutes  # retained in signature for API stability
    while True:
        try:
            repos = await state_manager.get_repos()
            if not repos:
                host.on_log("No repos configured.", "warn")
            else:
                host.on_log(f"Polling {len(repos)} repo(s)…", "info")
                for repo in repos:
                    try:
                        prs = await gh_list_prs(repo)
                    except Exception as e:
                        host.on_log(f"Failed to list PRs for {repo}: {e}", "error")
                        continue

                    try:
                        await git_update_pristine(repo)
                    except Exception as e:
                        host.on_log(f"Failed to fetch {repo}: {e}", "error")
                        continue

                    current_numbers = {str(p["number"]) for p in prs}

                    # Remove state + clones for PRs no longer in the list.
                    for old_num, _ in (await state_manager.get_all_pr_states(repo)).items():
                        if old_num not in current_numbers:
                            deleted = remove_clone(get_clone_path(repo, int(old_num)))
                            if deleted:
                                await state_manager.remove_pr(repo, old_num)
                                host.on_log(f"Removed PR #{old_num} ({repo}) from state", "info")
                            else:
                                host.on_log(
                                    f"PR #{old_num} ({repo}) gone from gh pr list but clone is recent — keeping state",
                                    "warn",
                                )

                    # Adopt local branches that now have PRs.
                    pr_branches = {p["headRefName"] for p in prs}
                    for branch in await state_manager.get_local_branches(repo):
                        if branch in pr_branches:
                            await state_manager.remove_local_branch(repo, branch)
                            host.on_log(f"Branch {branch} ({repo}) now has a PR — adopted", "info")

                    # Ensure stub state for new PRs.
                    for pr_data in prs:
                        pn = str(pr_data["number"])
                        existing = await state_manager.get_pr_state(repo, pn)
                        if existing is None:
                            existing = PRState(
                                title=pr_data.get("title", ""),
                                branch=pr_data["headRefName"],
                                created_at=pr_data.get("createdAt", ""),
                            )
                            await state_manager.upsert_pr_state(repo, pn, existing)

                    # Per-PR status refresh.
                    for pr_data in prs:
                        pn = str(pr_data["number"])
                        try:
                            await git_setup_pr_clone(repo, int(pn), pr_data["headRefName"])
                            clone = get_clone_path(repo, int(pn))
                            status = await compute_pr_status(repo, pr_data, clone)
                        except Exception as e:
                            host.on_log(f"Status check failed for #{pn} ({repo}): {e}", "warn")
                            status = "error"

                        st = await state_manager.get_pr_state(repo, pn) or PRState(
                            title=pr_data.get("title", ""),
                            branch=pr_data["headRefName"],
                            created_at=pr_data.get("createdAt", ""),
                        )
                        st.title = pr_data.get("title", st.title)
                        st.branch = pr_data["headRefName"]
                        st.created_at = pr_data.get("createdAt", st.created_at)
                        st.is_draft = pr_data.get("isDraft", False)
                        st.review_decision = pr_data.get("reviewDecision", "") or ""
                        comments = pr_data.get("comments", []) or []
                        reviews = pr_data.get("reviews", []) or []
                        st.comment_count = len(comments) + len(reviews)
                        st.review_count = len(reviews)
                        timestamps = (
                            [c.get("createdAt", "") for c in comments]
                            + [r.get("submittedAt", "") for r in reviews]
                        )
                        st.latest_activity = max(timestamps) if timestamps else None
                        st.status = status
                        st.error_message = None
                        await state_manager.upsert_pr_state(repo, pn, st)
                        host.on_status_update(repo, int(pn), status, None)

                    host.on_pr_list(await build_display_list(await state_manager.get_repos(), state_manager))

            host.on_pr_list(await build_display_list(await state_manager.get_repos(), state_manager))

        except asyncio.CancelledError:
            return
        except Exception as e:
            host.on_log(f"Poll loop error: {e}", "error")

        sleep_minutes = poll_interval_minutes
        if nudge is not None:
            nudge.clear()
            try:
                await asyncio.wait_for(nudge.wait(), timeout=sleep_minutes * 60)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(sleep_minutes * 60)
```

- [ ] **Step 4: Delete `processor.py`**

```bash
git rm pr_manager/processor.py
```

- [ ] **Step 5: Update `tests/test_cleanup_safety.py`**

Remove both occurrences of `patch.object(poll_module, "PRProcessor", MagicMock())` (currently lines 85 and 163). Also remove the `test_disabled_pr_clone_not_deleted_when_still_on_github` test entirely — the "disabled" concept no longer exists. The remaining two tests (`test_remove_clone_*` and `test_cleanup_does_not_remove_clone_for_pr_missing_from_gh_list`) must stay green.

The remaining cleanup test will need its patches extended because the new poll loop also calls `gh_pr_check_status`, `git_setup_pr_clone`, and `git_commits_behind`. Update the `with` block in `test_cleanup_does_not_remove_clone_for_pr_missing_from_gh_list` to:

```python
    with (
        patch.object(poll_module, "gh_list_prs", AsyncMock(return_value=fake_prs)),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "remove_clone", tracking_remove_clone),
        patch.object(poll_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
        patch.object(poll_module.asyncio, "sleep", _fake_sleep),
    ):
```

- [ ] **Step 6: Update `tests/test_poll_nudge.py`**

Remove the `processor_class`/`processor_instance` MagicMock setup (lines 48-51) and the `patch.object(poll_module, "PRProcessor", processor_class)` line (66). Extend the `with` block with the same status-call stubs as in the cleanup test:

```python
    with (
        patch.object(poll_module, "gh_list_prs", tracking_gh_list_prs),
        patch.object(poll_module, "git_update_pristine", AsyncMock()),
        patch.object(poll_module, "git_setup_pr_clone", AsyncMock()),
        patch.object(poll_module, "git_commits_behind", AsyncMock(return_value=0)),
        patch.object(poll_module, "gh_pr_check_status", AsyncMock(return_value=("green", ""))),
    ):
```

The test asserts two cycles happen — it should still pass because `nudge.set()` interrupts the `asyncio.wait_for(nudge.wait(), …)` path in the new loop.

- [ ] **Step 7: Run the full test suite**

```bash
cd /workspace && uv run pytest
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Make poll loop status-only; delete PRProcessor

The poll loop no longer decides when to rebase or fix CI; it only
reads GitHub check status and the behind-count per PR and writes the
observed status to PRState for display. All auto-write machinery
(rebase, CI fix, bot-commit reattribution) is gone from the loop;
the explicit `pr-manager fix <url>` path is now the sole writer.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Wire the "fixing" overlay through the TUI

The TUI now needs to (a) detect that a fix session is running for a PR, and (b) render `fixing` regardless of what the poll loop stored.

**Files:**
- Modify: `pr_manager/tui.py` — add `action_fix`, new `f` binding, overlay logic.
- Modify: `pr_manager/display.py` — stop inferring `is_active` from `PRState.status`.

- [ ] **Step 1: Add the `f` binding and `action_fix`**

In `pr_manager/tui.py`:

- Add to the `BINDINGS` list (around line 398, after `c`):
  ```python
          Binding("f", "fix", "fix PR"),
  ```
- Add the `action_fix` method. Put it next to `action_open_claude_session`:

  ```python
      async def action_fix(self) -> None:
          if not self._check_tmux():
              return
          pr = self._get_selected_pr()
          if not pr:
              self.post_message(AppLogMessage("No PR selected", "warn"))
              return
          if pr.number == 0:
              self.post_message(AppLogMessage(
                  "Local branches have no PR to fix yet", "warn",
              ))
              return
          worktree = self._resolve_worktree(pr)
          if not worktree.exists():
              self.post_message(AppLogMessage(
                  f"Worktree not yet created for {pr.branch} — try again after first poll",
                  "warn",
              ))
              return
          url = f"https://github.com/{pr.repo}/pull/{pr.number}"
          script_dir = os.path.dirname(os.path.abspath(__file__))
          inner_parts = [
              "uv", "run", "--project", script_dir + "/..",
              "pr-manager", "fix", url,
          ]
          import shlex
          inner_cmd = " ".join(shlex.quote(p) for p in inner_parts)
          wrapped = (
              f'{inner_cmd} || {{ '
              f'rc=$?; echo; echo "pr-manager fix exited with code $rc"; '
              f'echo "Press enter to close..."; read _; '
              f'}}'
          )
          window_name = f"fix-{pr.number}"
          rc, _, stderr = await run_cmd([
              "tmux", "new-window",
              "-c", str(worktree),
              "-n", window_name,
              "sh", "-c", wrapped,
          ], check=False)
          if rc != 0:
              self.post_message(AppLogMessage(
                  f"tmux new-window failed (rc={rc}): {stderr}", "error",
              ))
              return
          sentinel = asyncio.create_task(watch_tmux_window(window_name))
          self._active_tasks[(pr.repo, pr.number)] = sentinel
          self.post_message(AppLogMessage(
              f"Started fix session for PR #{pr.number} ({pr.repo})", "info",
          ))
          # Force an immediate table re-render so the `fixing` overlay shows
          # without waiting for the next spinner tick.
          self._refresh_table()
  ```

- [ ] **Step 2: Overlay `fixing` status in the TUI render path**

In `pr_manager/tui.py`, the `_refresh_table` method builds each row from `PRDisplayInfo`. Change it so that when `(pr.repo, pr.number)` has a live sentinel in `_active_tasks`, the row's status becomes `fixing` and `is_active` becomes `True`.

Locate `_refresh_table` (around line 481) and replace the loop body with:

```python
        for pr in self._display_prs:
            key = (pr.repo, pr.number)
            task = self._active_tasks.get(key)
            is_fixing = bool(task and not task.done())
            status = "fixing" if is_fixing else pr.status
            is_active = is_fixing or pr.is_active
            table.add_row(
                str(pr.number) if pr.number else "—",
                pr.repo,
                pr.branch,
                self._format_status(status, is_active),
                self._format_review(pr.review_status),
                pr.activity,
                pr.age,
                key=f"{pr.repo}:{pr.number or pr.branch}",
            )
```

Also update `handle_pr_status_update` (around line 510) so it no longer computes `is_active` from `("rebasing", "fixing_ci")` — those statuses no longer exist:

```python
    @on(PrStatusUpdate)
    def handle_pr_status_update(self, message: PrStatusUpdate) -> None:
        for i, pr in enumerate(self._display_prs):
            if pr.repo == message.repo and pr.number == message.pr_number:
                self._display_prs[i] = PRDisplayInfo(
                    repo=pr.repo,
                    number=pr.number,
                    title=pr.title,
                    branch=pr.branch,
                    status=message.status,
                    age=pr.age,
                    is_active=False,
                    error_message=message.error,
                    review_status=pr.review_status,
                    activity=pr.activity,
                )
                break
        self._refresh_table()
```

- [ ] **Step 3: Stop inferring `is_active` from `PRState.status` in `display.py`**

In `pr_manager/display.py`, the line:

```python
                is_active=pr_state.status in ("rebasing", "fixing_ci"),
```

becomes:

```python
                is_active=False,
```

The TUI overlays `is_active` based on the sentinel; `display.build_display_list` is source-of-truth for everything else.

- [ ] **Step 4: Run the tests**

```bash
cd /workspace && uv run pytest
```

Expected: all pass.

- [ ] **Step 5: Manual smoke test**

Run the TUI, confirm:

- The PR table populates with `green` / `pending` / `failing` / `behind` / `no_checks` statuses.
- Pressing `f` on a PR row opens a tmux window named `fix-<N>` running `pr-manager fix <url>`.
- The PR row in the main TUI shows a spinner + `fixing` for the duration of the tmux window.
- Closing the tmux window (or letting the fix exit cleanly) causes the row to revert to its observed status after the next poll.
- Pressing `x` on a local-branch row removes it; pressing `x` on a PR row logs the "`x` only removes local branches" message.

(If you cannot run the TUI in the current environment, record this as an open item and continue.)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Add `f` TUI binding and fix-session overlay

`f` opens a tmux window running `pr-manager fix <url>` in the PR's
clone and registers a watch-tmux sentinel in `_active_tasks`. The TUI
renders `fixing` + spinner for rows whose sentinel is still alive,
regardless of what the poll loop stored.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Delete the now-unused git helpers

`git_is_ancestor` and `gh_get_recent_commits` were only called from the deleted `PRProcessor`.

**Files:**
- Modify: `pr_manager/git.py` (delete two functions).

- [ ] **Step 1: Verify nothing else calls them**

```bash
cd /workspace && uv run grep -rn "git_is_ancestor\|gh_get_recent_commits" pr_manager tests > /tmp/refs.txt
```

Read `/tmp/refs.txt`. Expected: only the definitions in `pr_manager/git.py`.

- [ ] **Step 2: Delete both functions**

In `pr_manager/git.py`:

- Delete `gh_get_recent_commits` (currently lines 97-109).
- Delete `git_is_ancestor` (currently lines 185-191).

- [ ] **Step 3: Run the tests**

```bash
cd /workspace && uv run pytest
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add pr_manager/git.py
git commit -m "$(cat <<'EOF'
Delete unused git helpers (is_ancestor, recent_commits)

Only the removed PRProcessor called these.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Update the README

**Files:**
- Modify: `README.md`.

- [ ] **Step 1: Rewrite the "What it does" section**

Replace the bullet list currently at `README.md:9-14` with:

```markdown
pr-manager polls your open GitHub PRs and shows their current state:

- **Rebase / CI status** — shows whether each PR is behind its base,
  waiting on checks, failing, or green.
- **Review status** — draft, approved, changes requested, comment
  activity at a glance.

pr-manager never modifies your PRs on its own. When you want it to
act, invoke the fix command explicitly (TUI `f` or
`pr-manager fix <url>`). The fix command runs a Claude agent that
rebases onto the PR's base, fixes failing CI, and re-attributes bot
commits, looping until CI is green.
```

- [ ] **Step 2: Update the keybinding table**

Add an `f` row to the keybindings table at `README.md:63-74` (sorted roughly alphabetically):

```markdown
| `f` | Fix selected PR (opens tmux window running `pr-manager fix`) |
```

Update the `x` row from disabling PRs to removing local branches:

```markdown
| `x` | Remove selected local branch from the list |
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document the explicit fix command in README

The poll loop no longer performs writes automatically; the fix
command (CLI or `f` TUI binding) is now the only path that modifies
a PR.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Run the test suite**

```bash
cd /workspace && uv run pytest
```

Expected: all pass.

- [ ] **Step 2: Spot-check the diff**

```bash
cd /workspace && git log --oneline main..HEAD
cd /workspace && git diff main..HEAD --stat
```

Confirm: `pr_manager/processor.py`, `tests/test_disable_pr.py`, and `tests/test_user_session_guard.py` are deleted; `pr_manager/poll.py`, `pr_manager/tui.py`, `pr_manager/state.py`, `pr_manager/constants.py`, `pr_manager/git.py`, `pr_manager/display.py`, `pr_manager/assistant_api.py`, `README.md` are modified.

- [ ] **Step 3: Manual smoke test (if feasible)**

Run the TUI, press `f` on a PR, confirm the fix window opens, confirm the row displays `fixing` while the window is alive, confirm it reverts to observed status afterwards.
