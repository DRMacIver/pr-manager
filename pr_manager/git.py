from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

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


# ── Path helpers ─────────────────────────────────────────────────────────────

def get_repo_path(repo: str) -> Path:
    return REPOS_DIR / repo.replace("/", "-")


def get_worktree_path(repo: str, pr_number: int) -> Path:
    return get_repo_path(repo) / f"pr-{pr_number}"


def get_branch_worktree_path(repo: str, branch: str) -> Path:
    return get_repo_path(repo) / f"branch-{branch.replace('/', '-')}"


def get_log_path(repo: str, pr_number: int) -> Path:
    return LOGS_DIR / repo.replace("/", "-") / f"pr-{pr_number}.log"


# ── GitHub (via gh CLI) ──────────────────────────────────────────────────────

async def gh_list_prs(repo: str) -> list[dict]:
    _, out, _ = await run_cmd([
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--author", "@me",
        "--json", "number,title,headRefName,headRefOid,createdAt",
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


# ── Git ──────────────────────────────────────────────────────────────────────

async def git_clone_or_fetch(repo: str, local_path: Path) -> None:
    ssh_url = f"git@github.com:{repo}.git"
    if (local_path / ".git").exists():
        await run_cmd(["git", "remote", "set-url", "origin", ssh_url], cwd=local_path)
        await run_cmd(["git", "fetch", "origin", "--prune"], cwd=local_path)
    else:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await run_cmd([
            "git", "clone", f"git@github.com:{repo}.git", str(local_path),
        ])


async def git_create_new_branch_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    """Create a new branch from origin/main and set up a worktree for it."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await run_cmd(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), "origin/main"],
        cwd=repo_path,
    )


async def git_setup_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    if worktree_path.exists():
        return
    await run_cmd(
        ["git", "branch", "--track", branch, f"origin/{branch}"],
        cwd=repo_path, check=False,
    )
    await run_cmd(
        ["git", "worktree", "add", str(worktree_path), branch],
        cwd=repo_path,
    )


async def git_commits_behind_main(worktree_path: Path, branch: str) -> int:
    """Check how far the *remote* PR branch is behind origin/main."""
    rc, out, _ = await run_cmd(
        ["git", "rev-list", "--count", f"origin/{branch}..origin/main"],
        cwd=worktree_path, check=False,
    )
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


async def git_get_current_sha(worktree_path: Path) -> str:
    _, out, _ = await run_cmd(["git", "rev-parse", "HEAD"], cwd=worktree_path)
    return out.strip()


async def git_get_new_commits_since(worktree_path: Path, old_sha: str) -> list[str]:
    _, out, _ = await run_cmd(
        ["git", "log", "--format=%H", f"{old_sha}..HEAD"],
        cwd=worktree_path, check=False,
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


async def git_reattribute_and_push(worktree_path: Path, branch: str) -> bool:
    """Pull the latest remote commit, reattribute it to the local user, and push."""
    # Update worktree to match remote.
    rc, _, _ = await run_cmd(
        ["git", "reset", "--hard", f"origin/{branch}"],
        cwd=worktree_path, check=False,
    )
    if rc != 0:
        return False
    # Amend the commit to use the local user's identity, triggering a new SHA.
    rc, _, _ = await run_cmd(
        ["git", "commit", "--amend", "--no-edit", "--reset-author"],
        cwd=worktree_path, check=False,
    )
    if rc != 0:
        return False
    return await git_push_force_with_lease(worktree_path, branch)


async def git_push_force_with_lease(worktree_path: Path, branch: str) -> bool:
    rc, _, _ = await run_cmd(
        ["git", "push", "origin", branch, "--force-with-lease"],
        cwd=worktree_path, check=False,
    )
    return rc == 0
