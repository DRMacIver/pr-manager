# Explicit PR-fix command

## Problem

The poll loop currently decides autonomously when to rebase, fix failing CI,
or reattribute bot commits for each managed PR. The decision logic has grown
complex: "is there a recent human commit?", "is there an active Claude
session?", "is this PR stacked on another?", "is the parent green?", "are the
checks absent because a bot pushed?". Getting this right under all
combinations has been a persistent source of incorrect auto-fixes and skipped
work. It is easier to make the trigger explicit than to keep tuning the
implicit one.

## Goal

Replace the auto-fix decision logic with an explicit user command, so that
the only way pr-manager writes to a PR is when the user asks it to.

## Design

### Entry points

- **CLI (unchanged):** `pr-manager fix <url>` already exists and already does
  rebase → CI fix → loop until green, with bot-commit reattribution handled
  along the way. It stays as the single source of truth for the fix flow.
- **TUI (new):** binding `f` on a selected PR opens a tmux window named
  `fix-<pr_number>` running `uv run pr-manager fix <url>` in the PR's clone
  directory. Command is wrapped with the same "press enter to close on
  failure" pattern already used by `c` so the window stays visible if the
  fix command exits with an error.

### Poll loop

The poll loop becomes status-only. Every cycle it:

1. Lists open PRs per managed repo (`gh_list_prs`).
2. Updates the pristine clone (`git_update_pristine`).
3. Adopts local branches that now have PRs (moves them out of
   `local_branches`).
4. Ensures every known PR has stub state so it appears in the display.
5. Removes state and clones for PRs no longer returned by `gh` (subject to
   the existing "recent clone → keep" guard).
6. For each live PR: ensures the PR clone exists (`git_setup_pr_clone`);
   reads `gh_pr_check_status` and `git_commits_behind(branch, baseRefName)`;
   writes the computed status back to `PRState.status` so the display
   reflects it.
7. Builds and emits the display list.
8. Sleeps `poll_interval_minutes` (fixed cadence; the adaptive 1-minute
   sleep when any PR is `pending`/`blocked` is removed).

No `PRProcessor` invocation. No per-PR asyncio tasks spawned by the poll
loop. No git writes, no pushes, no agent runs — setting up the clone is
read-only from GitHub's perspective.

### TUI fix binding

Pressing `f` on a selected PR:

1. Requires `TMUX` env (same precondition as `c`, `o`, `v`, `n`).
2. Ensures the PR clone exists; warns if the first poll hasn't created it
   yet.
3. Runs `tmux new-window -c <clone> -n fix-<pr_number> sh -c '<wrapped>'`
   where `<wrapped>` is `uv run pr-manager fix <url> || { echo ...; read; }`.
4. Installs a `watch_tmux_window("fix-<pr_number>")` sentinel task into
   `_active_tasks[(repo, pr_number)]` so the TUI knows a fix session is
   live.

### Status display

`PRState.status` is populated by the poll loop with one of:

- `green` — checks pass, not behind base.
- `pending` — checks still running.
- `failing` — checks red.
- `behind` — any commits behind base (regardless of check state; takes
  precedence over check state so the user sees the rebase need).
- `no_checks` — no checks reported yet.

The TUI overlays a `fixing` state on top of the stored status: when
`_active_tasks[(repo, pr_number)]` holds a live `watch_tmux_window`
sentinel (from pressing `f` or `c`), the row shows spinner + "fixing"
regardless of the underlying `PRState.status`. When the sentinel task
completes (window closed) the row reverts to the stored status from the
next poll cycle.

The helper that computes status values lives in `poll.py` (it is only
called from the poll loop). `display.py` continues to render
`PRDisplayInfo` and its `is_active` field now reads from the `fixing`
overlay rather than from `PRState.status`.

### State shape

`PRState` loses `stacked_on`. `AppState` loses `disabled_prs`.

- `our_commits` stays — `run_fix` continues to record our pushes and the
  TUI detail screen still reports the count.
- State loader keeps reading `disabled_prs` / `hidden_prs` keys from
  existing state files silently (no migration step, no crash on startup).

### Files and functions

**Deleted:**
- `pr_manager/processor.py` (entire file, including `PRProcessor`,
  `has_active_claude_session`, `_parse_pr_references`, the stack
  detection, the human-changes check).

**Removed from other modules:**
- `poll.py` — all `PRProcessor` integration, `_active_tasks` writes from
  poll, `disabled_prs` filtering, adaptive sleep.
- `state.py` — `disable_pr`, `enable_pr`, `is_disabled`,
  `get_disabled_prs`, `stacked_on` field, `disabled_prs` field on
  `AppState`.
- `tui.py` — the PR-row branch of `action_toggle_disabled` (the
  local-branch-removal branch stays on `x`, see "`x` binding" below).
- `display.py` — any rendering of `disabled` / `blocked` status.
- `constants.py` — any entries in `STATUS_STYLE` for removed statuses
  (`rebasing`, `fixing_ci`, `blocked`, `human_changes` — replaced by the
  single `fixing` state).

**Added:**
- `tui.py` — `action_fix` and a new `f` binding; helper to spawn the fix
  tmux window and register its sentinel.
- `poll.py` — the "compute row status from git + gh" helper that used to
  live in `PRProcessor`.

### `x` binding

`x` today handles two things: remove a local branch from the list, and
disable/enable a PR. PR disable goes away. `x` keeps its local-branch
removal behaviour and does nothing when a PR row is selected.

### Concurrency with `pr-manager fix`

The TUI and a running `pr-manager fix <url>` subprocess both load the same
state file (`STATE_PATH`). Writes are already atomic-rename, but two
processes writing concurrently can still clobber each other (last writer
wins). This pre-exists and is unchanged by this design. The subprocess
writes `our_commits`; the TUI writes observed status. Clobbers are
low-impact because observed status is re-derived every poll cycle.

## Testing

The repo has no existing test suite. Verification is manual:

1. Start the TUI, confirm the PR list populates and status reflects the
   actual GitHub state (green/pending/failing/behind) without any agent
   activity in the background.
2. Press `f` on a PR that is behind main with failing CI. Confirm a tmux
   window opens, `run_fix` rebases, fixes, and pushes.
3. While the fix is running, confirm the TUI row shows the spinner +
   "fixing".
4. After the fix completes (or the window is closed), confirm the row
   reverts to the observed GitHub state.
5. Confirm that loading an existing state file with `disabled_prs` /
   `stacked_on` populated does not crash, and that those fields are
   silently dropped on the next save.
