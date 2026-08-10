from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .constants import REPOS_DIR, LOGS_DIR

log = logging.getLogger(__name__)


# Generous ceiling: even `git clone` of a large repo should finish well
# inside this, while a genuinely hung gh/git/tmux call can no longer
# freeze the poll loop (and with it all status updates) forever.
_DEFAULT_CMD_TIMEOUT = 600.0


async def run_cmd(
    args: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    timeout: float = _DEFAULT_CMD_TIMEOUT,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        message = f"`{' '.join(args)}` timed out after {timeout:.0f}s and was killed"
        if check:
            raise RuntimeError(message) from None
        return 124, "", message
    rc = proc.returncode or 0
    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()
    if check and rc != 0:
        raise RuntimeError(f"`{' '.join(args)}` failed (rc={rc}): {stderr}")
    return rc, stdout, stderr


def _ssh_url(repo: str) -> str:
    return f"git@github.com:{repo}.git"


# ── Path helpers ─────────────────────────────────────────────────────────────

def get_pristine_path(repo: str) -> Path:
    """The cached pristine clone — never worked in directly."""
    return REPOS_DIR / repo.replace("/", "-") / "pristine"


def get_clone_path(repo: str, pr_number: int) -> Path:
    """Working clone for a PR."""
    return REPOS_DIR / repo.replace("/", "-") / f"pr-{pr_number}"


def get_branch_clone_path(repo: str, branch: str) -> Path:
    """Working clone for a local branch."""
    return REPOS_DIR / repo.replace("/", "-") / f"branch-{branch.replace('/', '-')}"


def get_log_path(repo: str, pr_number: int) -> Path:
    return LOGS_DIR / repo.replace("/", "-") / f"pr-{pr_number}.log"


def get_branch_log_path(repo: str, branch: str) -> Path:
    """Log path for a local branch that has no PR (and no PR number) yet."""
    return LOGS_DIR / repo.replace("/", "-") / f"branch-{branch.replace('/', '-')}.log"


# ── GitHub (via gh CLI) ──────────────────────────────────────────────────────

async def gh_list_prs(repo: str) -> list[dict]:
    _, out, _ = await run_cmd([
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--author", "@me",
        "--limit", "300",
        "--json", "number,title,headRefName,baseRefName,headRefOid,createdAt,isDraft,reviewDecision,comments,reviews,body",
    ])
    return json.loads(out) if out else []


# Check states that mean "this check passed" / "this check is still running".
# Anything outside these two sets — FAILURE, ERROR, CANCELLED, TIMED_OUT,
# ACTION_REQUIRED, STARTUP_FAILURE, STALE, or a state we've never heard of —
# counts as failing: an unknown state must never read as success.
_PASSING_CHECK_STATES = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
_RUNNING_CHECK_STATES = frozenset(
    {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
)


async def gh_pr_check_status(repo: str, pr_number: int) -> tuple[str, str]:
    """Return ("green" | "pending" | "failing" | "no_checks" | "error", details)."""
    rc, out, stderr = await run_cmd([
        "gh", "pr", "checks", str(pr_number), "--repo", repo,
        "--json", "name,state",
    ], check=False)
    if rc != 0:
        if "no checks reported" in (stderr or "").lower() or "no checks reported" in (out or "").lower():
            return "no_checks", ""
        return "error", (stderr or out or "gh pr checks failed").strip()
    if not out:
        return "no_checks", ""
    checks = json.loads(out)
    if not checks:
        return "no_checks", ""
    failures = [
        c for c in checks
        if c.get("state") not in _PASSING_CHECK_STATES | _RUNNING_CHECK_STATES
    ]
    if failures:
        details = "\n".join(f"- {c['name']}: {c['state']}" for c in failures)
        return "failing", details
    if any(c.get("state") in _RUNNING_CHECK_STATES for c in checks):
        return "pending", ""
    return "green", ""


# ── Pristine clone management ───────────────────────────────────────────────

async def git_update_pristine(repo: str) -> None:
    """Ensure the pristine clone exists and is up-to-date."""
    pristine = get_pristine_path(repo)
    ssh = _ssh_url(repo)
    if (pristine / ".git").exists():
        await run_cmd(["git", "remote", "set-url", "origin", ssh], cwd=pristine)
        await run_cmd(["git", "fetch", "origin", "--prune"], cwd=pristine)
    else:
        pristine.parent.mkdir(parents=True, exist_ok=True)
        await run_cmd(["git", "clone", ssh, str(pristine)])


# ── Working clone management ────────────────────────────────────────────────

async def _clone_from_pristine(repo: str, clone_path: Path) -> None:
    """Clone from the pristine cache and set remote to the real origin."""
    pristine = get_pristine_path(repo)
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    await run_cmd(["git", "clone", str(pristine), str(clone_path)])
    await run_cmd(["git", "remote", "set-url", "origin", _ssh_url(repo)], cwd=clone_path)


async def git_setup_pr_clone(repo: str, pr_number: int, branch: str) -> None:
    """Ensure a working clone exists for a PR branch."""
    clone_path = get_clone_path(repo, pr_number)
    if clone_path.exists():
        return
    if clone_path.is_symlink():
        clone_path.unlink()
    # If a branch clone already exists (e.g. a local branch that just got a
    # PR), symlink to it instead of creating a fresh clone.  This preserves
    # any active Claude sessions or other processes in the original directory.
    branch_clone = get_branch_clone_path(repo, branch)
    if branch_clone.exists():
        clone_path.symlink_to(branch_clone.resolve())
        return
    await _clone_from_pristine(repo, clone_path)
    # The clone is made from the local pristine cache, whose remote-tracking
    # refs are not carried over as branches, so the PR branch is absent until
    # we fetch from the real origin.  The checkout must NOT be check=False:
    # silently failing leaves the clone on the default branch (main), and a
    # later `git rebase origin/main` would then rebase main itself.
    await run_cmd(["git", "fetch", "origin", "--prune"], cwd=clone_path)
    await run_cmd(["git", "checkout", branch], cwd=clone_path)


class DirtyWorkingTreeError(RuntimeError):
    """The working tree has uncommitted changes we refuse to destroy."""


async def git_sync_branch_to_origin(clone_path: Path, branch: str) -> bool:
    """Make the local branch exactly match origin/<branch>.

    The remote is the source of truth: local-only COMMITS (stale state
    from an earlier run, leftovers of a crashed agent) are discarded.
    Without this, an agent can rebase a stale local branch and — because
    the loop fetches first, re-arming the force-with-lease lease —
    force-push commits that humans pushed in the meantime out of
    existence.

    Uncommitted changes are a different matter: the clone may be a
    symlink into the user's branch clone with an interactive session in
    it, and reset --hard would silently eat their edits — so a dirty
    tree raises DirtyWorkingTreeError instead.

    Returns False when origin/<branch> no longer exists.
    """
    if await asyncio.to_thread(_has_uncommitted_changes, clone_path):
        raise DirtyWorkingTreeError(
            f"{clone_path} has uncommitted changes — refusing to reset"
        )
    await run_cmd(["git", "fetch", "origin", "--prune"], cwd=clone_path)
    rc, _, _ = await run_cmd(
        ["git", "rev-parse", "--verify", f"origin/{branch}"],
        cwd=clone_path, check=False,
    )
    if rc != 0:
        return False
    await run_cmd(["git", "checkout", branch], cwd=clone_path)
    await run_cmd(["git", "reset", "--hard", f"origin/{branch}"], cwd=clone_path)
    return True


async def git_default_branch(clone_path: Path) -> str:
    """Return the default branch name for origin ('main' or 'master')."""
    rc, _, _ = await run_cmd(
        ["git", "rev-parse", "--verify", "origin/main"],
        cwd=clone_path, check=False,
    )
    if rc == 0:
        return "main"
    return "master"


async def git_create_branch_clone(repo: str, branch: str) -> Path:
    """Create a working clone with a new branch from the default branch."""
    clone_path = get_branch_clone_path(repo, branch)
    await _clone_from_pristine(repo, clone_path)
    await run_cmd(["git", "fetch", "origin", "--prune"], cwd=clone_path)
    default = await git_default_branch(clone_path)
    await run_cmd(["git", "checkout", "-b", branch, f"origin/{default}"], cwd=clone_path)
    return clone_path


_ONE_DAY = 86400


def _newest_mtime(root: Path) -> float:
    """Newest mtime of any entry in the tree (the root's own mtime doesn't
    change when nested files do)."""
    newest = root.stat().st_mtime
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            try:
                mtime = os.lstat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            newest = max(newest, mtime)
    return newest


def _has_uncommitted_changes(clone_path: Path) -> bool:
    """True if the git tree is dirty — or if we can't tell (be conservative)."""
    if not (clone_path / ".git").exists():
        return False
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=clone_path, capture_output=True, text=True,
    )
    return status.returncode != 0 or bool(status.stdout.strip())


def remove_clone(clone_path: Path) -> bool:
    """Remove a working clone directory.

    Returns True if the directory was deleted, False if it was kept.
    Safety nets, in order:
    - never delete a tree with uncommitted git changes (unsaved work),
      regardless of age;
    - never delete a tree where any file was modified within the last day.

    Committed-but-unpushed work older than a day is not protected: with
    squash-merge workflows, local commits are routinely unreachable from
    remote refs, so an "unpushed commits" check would block cleanup of
    every merged PR forever.

    Symlinks (valid or broken) are always safe to unlink because the
    actual data lives in the target directory.
    """
    if clone_path.is_symlink():
        clone_path.unlink()
        return True
    if not clone_path.exists():
        return True
    if _has_uncommitted_changes(clone_path):
        log.warning(
            "Refusing to delete %s — tree has uncommitted changes", clone_path,
        )
        return False
    age = time.time() - _newest_mtime(clone_path)
    if age < _ONE_DAY:
        log.warning(
            "Refusing to delete %s — modified %.1f hours ago (< 24h)",
            clone_path, age / 3600,
        )
        return False
    shutil.rmtree(clone_path)
    return True


# ── Git queries & operations (run in working clones) ────────────────────────

async def git_commits_behind(clone_path: Path, branch: str, target: str = "main") -> int:
    """Check how far the *remote* PR branch is behind origin/<target>."""
    # Fetch to ensure we have latest refs in this clone.
    await run_cmd(["git", "fetch", "origin", "--prune"], cwd=clone_path, check=False)
    rc, out, _ = await run_cmd(
        ["git", "rev-list", "--count", f"origin/{branch}..origin/{target}"],
        cwd=clone_path, check=False,
    )
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


async def git_get_current_sha(clone_path: Path) -> str:
    _, out, _ = await run_cmd(["git", "rev-parse", "HEAD"], cwd=clone_path)
    return out.strip()


async def git_get_new_commits_since(clone_path: Path, old_sha: str) -> list[str]:
    _, out, _ = await run_cmd(
        ["git", "log", "--format=%H", f"{old_sha}..HEAD"],
        cwd=clone_path, check=False,
    )
    return [s.strip() for s in out.splitlines() if s.strip()]


async def git_latest_commit_is_bot(repo: str, branch: str) -> bool:
    """Check if the latest commit on the remote branch was authored by a bot."""
    owner, name = repo.split("/", 1)
    rc, out, _ = await run_cmd([
        "gh", "api",
        f"repos/{owner}/{name}/commits/{branch}",
        "--jq", ".commit.author.email",
    ], check=False)
    if rc != 0 or not out:
        return False
    email = out.strip()
    return "[bot]" in email or email.endswith("@users.noreply.github.com") and "bot" in email


async def git_reattribute_and_push(clone_path: Path, branch: str) -> bool:
    """Pull the latest remote commit, reattribute it to the local user, and push."""
    rc, _, _ = await run_cmd(
        ["git", "reset", "--hard", f"origin/{branch}"],
        cwd=clone_path, check=False,
    )
    if rc != 0:
        return False
    rc, _, _ = await run_cmd(
        ["git", "commit", "--amend", "--no-edit", "--reset-author"],
        cwd=clone_path, check=False,
    )
    if rc != 0:
        return False
    return await git_push_force_with_lease(clone_path, branch)


async def git_push_force_with_lease(clone_path: Path, branch: str) -> bool:
    rc, _, _ = await run_cmd(
        ["git", "push", "origin", branch, "--force-with-lease"],
        cwd=clone_path, check=False,
    )
    return rc == 0
