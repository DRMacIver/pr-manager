from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from .constants import REPOS_DIR, LOGS_DIR


async def run_cmd(
    args: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
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


# ── GitHub (via gh CLI) ──────────────────────────────────────────────────────

async def gh_list_prs(repo: str) -> list[dict]:
    _, out, _ = await run_cmd([
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--author", "@me",
        "--limit", "300",
        "--json", "number,title,headRefName,headRefOid,createdAt,isDraft,reviewDecision,comments,reviews,body",
    ])
    return json.loads(out) if out else []


async def gh_pr_check_status(repo: str, pr_number: int) -> tuple[str, str]:
    """Return ("green" | "pending" | "failing" | "no_checks", details)."""
    rc, out, stderr = await run_cmd([
        "gh", "pr", "checks", str(pr_number), "--repo", repo,
        "--json", "name,state",
    ], check=False)
    if rc != 0:
        if "no checks reported" in (stderr or "").lower() or "no checks reported" in (out or "").lower():
            return "no_checks", ""
        return "pending", ""
    if not out:
        return "no_checks", ""
    checks = json.loads(out)
    if not checks:
        return "no_checks", ""
    failures = [c for c in checks if c.get("state") == "FAILURE"]
    if failures:
        details = "\n".join(f"- {c['name']}: {c['state']}" for c in failures)
        return "failing", details
    in_progress = [c for c in checks if c.get("state") in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING")]
    if in_progress:
        return "pending", ""
    return "green", ""


async def gh_get_recent_commits(repo: str, branch: str, since_iso: str) -> list[str]:
    owner, name = repo.split("/", 1)
    rc, out, _ = await run_cmd([
        "gh", "api",
        f"repos/{owner}/{name}/commits",
        "--jq", ".[].sha",
        "-f", f"sha={branch}",
        "-f", f"since={since_iso}",
        "--paginate",
    ], check=False)
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


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
    # If a branch clone already exists (e.g. a local branch that just got a
    # PR), symlink to it instead of creating a fresh clone.  This preserves
    # any active Claude sessions or other processes in the original directory.
    branch_clone = get_branch_clone_path(repo, branch)
    if branch_clone.exists():
        clone_path.symlink_to(branch_clone.resolve())
        return
    await _clone_from_pristine(repo, clone_path)
    await run_cmd(["git", "checkout", branch], cwd=clone_path, check=False)


async def git_create_branch_clone(repo: str, branch: str) -> Path:
    """Create a working clone with a new branch from origin/main."""
    clone_path = get_branch_clone_path(repo, branch)
    await _clone_from_pristine(repo, clone_path)
    await run_cmd(["git", "checkout", "-b", branch, "origin/main"], cwd=clone_path)
    return clone_path


_ONE_DAY = 86400


def remove_clone(clone_path: Path) -> bool:
    """Remove a working clone directory.

    Returns True if the directory was deleted, False if it was kept.
    As a safety net, refuses to delete directories modified within the
    last day — logs a warning instead.
    """
    if not clone_path.exists():
        return True
    age = time.time() - clone_path.stat().st_mtime
    if age < _ONE_DAY:
        log.warning(
            "Refusing to delete %s — modified %.1f hours ago (< 24h)",
            clone_path, age / 3600,
        )
        return False
    shutil.rmtree(clone_path)
    return True


# ── Git queries & operations (run in working clones) ────────────────────────

async def git_is_ancestor(clone_path: Path, ancestor_branch: str, descendant_branch: str) -> bool:
    """Check if origin/<ancestor_branch> is an ancestor of origin/<descendant_branch>."""
    rc, _, _ = await run_cmd(
        ["git", "merge-base", "--is-ancestor", f"origin/{ancestor_branch}", f"origin/{descendant_branch}"],
        cwd=clone_path, check=False,
    )
    return rc == 0


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
