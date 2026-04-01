#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "textual>=0.80.0",
#   "claude-agent-sdk",
# ]
# ///
"""pr_manager.py - GitHub PR auto-manager with Claude agent integration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path("~/.local/share/pr-manager").expanduser()
REPOS_DIR = BASE_DIR / "repos"
LOGS_DIR = BASE_DIR / "logs"
STATE_PATH = BASE_DIR / "state.json"

SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# (icon, rich style) per status
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "idle":          ("○", "dim"),
    "rebasing":      ("◉", "yellow"),
    "fixing_ci":     ("◉", "yellow"),
    "green":         ("✓", "green"),
    "error":         ("✗", "red bold"),
    "human_changes": ("⚠", "blue"),
}


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PRState:
    session_id: Optional[str] = None
    our_commits: list[str] = field(default_factory=list)
    status: str = "idle"
    last_checked: Optional[str] = None
    error_message: Optional[str] = None
    title: str = ""
    branch: str = ""
    created_at: Optional[str] = None


@dataclass
class AppState:
    repos: list[str] = field(default_factory=list)
    # repo -> pr_number_str -> raw dict (serialised PRState)
    pr_state: dict[str, dict[str, dict]] = field(default_factory=dict)


@dataclass
class PRDisplayInfo:
    repo: str
    number: int
    title: str
    branch: str
    status: str
    age: str
    is_active: bool
    error_message: Optional[str]


# ──────────────────────────────────────────────────────────────────────────────
# State manager
# ──────────────────────────────────────────────────────────────────────────────

_PR_STATE_FIELDS = set(PRState.__dataclass_fields__)


def _dict_to_pr_state(d: dict) -> PRState:
    return PRState(**{k: v for k, v in d.items() if k in _PR_STATE_FIELDS})


class StateManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = AppState()

    async def load(self) -> None:
        async with self._lock:
            if STATE_PATH.exists():
                data = json.loads(STATE_PATH.read_text())
                self._state = AppState(
                    repos=data.get("repos", []),
                    pr_state=data.get("pr_state", {}),
                )
            else:
                self._state = AppState()

    def _save_sync(self) -> None:
        """Write state to disk atomically. Must be called while holding self._lock."""
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"repos": self._state.repos, "pr_state": self._state.pr_state},
            indent=2,
        ))
        os.replace(tmp, STATE_PATH)

    async def add_repo(self, repo: str) -> None:
        async with self._lock:
            if repo not in self._state.repos:
                self._state.repos.append(repo)
                self._save_sync()

    async def remove_repo(self, repo: str) -> None:
        async with self._lock:
            self._state.repos = [r for r in self._state.repos if r != repo]
            self._state.pr_state.pop(repo, None)
            self._save_sync()

    async def get_repos(self) -> list[str]:
        async with self._lock:
            return list(self._state.repos)

    async def get_pr_state(self, repo: str, pr_number: str) -> Optional[PRState]:
        async with self._lock:
            d = self._state.pr_state.get(repo, {}).get(str(pr_number))
            return _dict_to_pr_state(d) if d is not None else None

    async def get_all_pr_states(self, repo: str) -> dict[str, PRState]:
        async with self._lock:
            return {
                num: _dict_to_pr_state(d)
                for num, d in self._state.pr_state.get(repo, {}).items()
            }

    async def upsert_pr_state(self, repo: str, pr_number: str, state: PRState) -> None:
        async with self._lock:
            self._state.pr_state.setdefault(repo, {})[str(pr_number)] = asdict(state)
            self._save_sync()

    async def record_our_commits(self, repo: str, pr_number: str, shas: list[str]) -> None:
        async with self._lock:
            repo_map = self._state.pr_state.setdefault(repo, {})
            pr_dict = repo_map.setdefault(str(pr_number), {})
            existing = set(pr_dict.get("our_commits", []))
            existing.update(shas)
            pr_dict["our_commits"] = list(existing)
            self._save_sync()

    async def remove_pr(self, repo: str, pr_number: str) -> None:
        async with self._lock:
            self._state.pr_state.get(repo, {}).pop(str(pr_number), None)
            self._save_sync()


# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_repo_path(repo: str) -> Path:
    return REPOS_DIR / repo.replace("/", "-")


def get_worktree_path(repo: str, pr_number: int) -> Path:
    return get_repo_path(repo) / f"pr-{pr_number}"


def get_log_path(repo: str, pr_number: int) -> Path:
    return LOGS_DIR / repo.replace("/", "-") / f"pr-{pr_number}.log"


# ──────────────────────────────────────────────────────────────────────────────
# Git / GitHub helpers
# ──────────────────────────────────────────────────────────────────────────────

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


async def gh_list_prs(repo: str) -> list[dict]:
    _, out, _ = await run_cmd([
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--author", "@me",
        "--json", "number,title,headRefName,headRefOid,createdAt",
    ])
    return json.loads(out) if out else []


async def gh_pr_check_status(repo: str, pr_number: int) -> tuple[str, str]:
    """Return ("green" | "pending" | "failing", failure_details_str)."""
    rc, out, _ = await run_cmd([
        "gh", "pr", "checks", str(pr_number), "--repo", repo,
        "--json", "name,state,conclusion",
    ], check=False)
    if rc != 0 or not out:
        return "pending", ""
    checks = json.loads(out)
    if not checks:
        return "green", ""
    failures = [
        c for c in checks
        if c.get("state") in ("fail", "startup_failure")
        or c.get("conclusion") in ("failure", "timed_out", "action_required")
    ]
    if failures:
        details = "\n".join(f"- {c['name']}: {c.get('state') or c.get('conclusion')}" for c in failures)
        return "failing", details
    pending = [c for c in checks if c.get("state") == "pending" or c.get("conclusion") is None]
    if pending:
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


async def git_setup_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    if worktree_path.exists():
        await run_cmd(["git", "fetch", "origin", branch], cwd=worktree_path, check=False)
        return
    # Create a local tracking branch if it doesn't already exist.
    await run_cmd(
        ["git", "branch", "--track", branch, f"origin/{branch}"],
        cwd=repo_path, check=False,
    )
    await run_cmd(
        ["git", "worktree", "add", str(worktree_path), branch],
        cwd=repo_path,
    )


async def git_commits_behind_main(worktree_path: Path) -> int:
    rc, out, _ = await run_cmd(
        ["git", "rev-list", "--count", "HEAD..origin/main"],
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


async def git_push_force_with_lease(worktree_path: Path, branch: str) -> bool:
    rc, _, _ = await run_cmd(
        ["git", "push", "origin", branch, "--force-with-lease"],
        cwd=worktree_path, check=False,
    )
    return rc == 0


# ──────────────────────────────────────────────────────────────────────────────
# Agent runner
# ──────────────────────────────────────────────────────────────────────────────

class AgentRunner:
    def __init__(
        self,
        repo: str,
        pr_number: int,
        branch: str,
        worktree_path: Path,
        state_manager: StateManager,
        log_path: Path,
    ) -> None:
        self._repo = repo
        self._pr_number = pr_number
        self._branch = branch
        self._worktree_path = worktree_path
        self._state_manager = state_manager
        self._log_path = log_path

    async def run_rebase(self) -> bool:
        prompt = (
            f"PR #{self._pr_number} (branch: {self._branch}) needs to be rebased on top of main.\n"
            "Steps:\n"
            "1. Run: git fetch origin\n"
            "2. Run: git rebase origin/main\n"
            "3. Resolve any conflicts if they arise\n"
            "4. Once the rebase has succeeded, output exactly: DONE"
        )
        return await self._run_agent(prompt)

    async def run_ci_fix(self, failures: str) -> bool:
        prompt = (
            f"PR #{self._pr_number} (branch: {self._branch}) has failing CI checks:\n"
            f"{failures}\n\n"
            "Please examine the failures and fix the code so the CI will pass.\n"
            "Commit your changes when done (use git add -A && git commit).\n"
            "When complete, output exactly: DONE"
        )
        return await self._run_agent(prompt)

    async def _run_agent(self, prompt: str) -> bool:
        pr_state = await self._state_manager.get_pr_state(self._repo, str(self._pr_number))
        session_id = pr_state.session_id if pr_state else None

        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        options = ClaudeAgentOptions(
            cwd=str(self._worktree_path),
            allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            resume=session_id,
            max_turns=50,
        )

        found_done = False
        try:
            with open(self._log_path, "a", buffering=1) as log_f:
                ts = datetime.now().strftime("%H:%M:%S")
                log_f.write(f"\n[{ts}] === Agent started ===\n")

                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        new_sid = message.data.get("session_id")
                        if new_sid:
                            current = await self._state_manager.get_pr_state(
                                self._repo, str(self._pr_number)
                            ) or PRState()
                            current.session_id = new_sid
                            await self._state_manager.upsert_pr_state(
                                self._repo, str(self._pr_number), current
                            )

                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                ts = datetime.now().strftime("%H:%M:%S")
                                for line in block.text.splitlines():
                                    log_f.write(f"[{ts}] {line}\n")

                    elif isinstance(message, ResultMessage):
                        found_done = bool(message.result and "DONE" in message.result.upper())
                        ts = datetime.now().strftime("%H:%M:%S")
                        log_f.write(f"[{ts}] === Agent finished (DONE={found_done}) ===\n")

        except (CLINotFoundError, CLIConnectionError) as e:
            with open(self._log_path, "a") as log_f:
                log_f.write(f"[ERROR] Agent SDK error: {e}\n")
            return False
        except asyncio.CancelledError:
            with open(self._log_path, "a") as log_f:
                log_f.write("[INFO] Agent cancelled by user\n")
            raise
        except Exception as e:
            with open(self._log_path, "a") as log_f:
                log_f.write(f"[ERROR] Unexpected error: {e}\n")
            return False

        return found_done


# ──────────────────────────────────────────────────────────────────────────────
# PR processor
# ──────────────────────────────────────────────────────────────────────────────

class PRProcessor:
    def __init__(
        self,
        repo: str,
        pr_data: dict,
        state_manager: StateManager,
        status_cb: Callable[[str, int, str, Optional[str]], None],
        log_cb: Callable[[str, str], None],
    ) -> None:
        self._repo = repo
        self._pr_data = pr_data
        self._state_manager = state_manager
        self._status_cb = status_cb
        self._log_cb = log_cb

    async def process(self, recent_minutes: int) -> None:
        pr_number: int = self._pr_data["number"]
        branch: str = self._pr_data["headRefName"]
        title: str = self._pr_data.get("title", "")
        created_at: str = self._pr_data.get("createdAt", "")

        try:
            # Refresh cached display info.
            pr_state = await self._state_manager.get_pr_state(self._repo, str(pr_number)) or PRState()
            pr_state.title = title
            pr_state.branch = branch
            pr_state.created_at = created_at
            pr_state.last_checked = datetime.now(timezone.utc).isoformat()
            await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)

            repo_path = get_repo_path(self._repo)
            worktree_path = get_worktree_path(self._repo, pr_number)
            log_path = get_log_path(self._repo, pr_number)

            self._log_cb(f"Checking PR #{pr_number} ({self._repo}/{branch})", "info")
            await git_clone_or_fetch(self._repo, repo_path)
            await git_setup_worktree(repo_path, worktree_path, branch)

            # Re-read after potential worktree setup writes.
            pr_state = await self._state_manager.get_pr_state(self._repo, str(pr_number)) or PRState(
                title=title, branch=branch, created_at=created_at,
            )

            # 1. Skip if there are recent human commits.
            if await self._has_human_changes(branch, pr_state, recent_minutes):
                pr_state.status = "human_changes"
                pr_state.error_message = "Recent human commits — skipping"
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
                self._status_cb(self._repo, pr_number, "human_changes", None)
                return

            # 2. Rebase if behind main.
            behind = await git_commits_behind_main(worktree_path)
            if behind > 0:
                self._log_cb(
                    f"PR #{pr_number} is {behind} commit(s) behind main — rebasing", "info"
                )
                await self._do_rebase(pr_number, branch, worktree_path, pr_state, log_path)
                return

            # 3. Fix CI if failing.
            check_status, failures = await gh_pr_check_status(self._repo, pr_number)
            if check_status == "failing":
                self._log_cb(f"PR #{pr_number} has failing checks — fixing CI", "info")
                await self._do_ci_fix(pr_number, branch, worktree_path, pr_state, log_path, failures)
                return

            # 4. All good.
            pr_state.status = "green"
            pr_state.error_message = None
            await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
            self._status_cb(self._repo, pr_number, "green", None)
            self._log_cb(f"PR #{pr_number} ({self._repo}) ✓ green", "info")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_cb(f"Error processing PR #{pr_number} ({self._repo}): {e}", "error")
            self._status_cb(self._repo, pr_number, "error", str(e))
            try:
                s = await self._state_manager.get_pr_state(self._repo, str(pr_number)) or PRState()
                s.status = "error"
                s.error_message = str(e)
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), s)
            except Exception:
                pass

    async def _has_human_changes(
        self, branch: str, pr_state: PRState, recent_minutes: int
    ) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=recent_minutes)
        recent_shas = await gh_get_recent_commits(
            self._repo, branch, cutoff.isoformat()
        )
        our_set = set(pr_state.our_commits)
        return any(sha not in our_set for sha in recent_shas)

    async def _do_rebase(
        self,
        pr_number: int,
        branch: str,
        worktree_path: Path,
        pr_state: PRState,
        log_path: Path,
    ) -> None:
        pr_state.status = "rebasing"
        pr_state.error_message = None
        await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
        self._status_cb(self._repo, pr_number, "rebasing", None)

        old_sha = await git_get_current_sha(worktree_path)
        runner = AgentRunner(
            self._repo, pr_number, branch, worktree_path, self._state_manager, log_path
        )
        success = await runner.run_rebase()

        if success:
            pushed = await git_push_force_with_lease(worktree_path, branch)
            if pushed:
                new_commits = await git_get_new_commits_since(worktree_path, old_sha)
                await self._state_manager.record_our_commits(
                    self._repo, str(pr_number), new_commits
                )
                pr_state = (
                    await self._state_manager.get_pr_state(self._repo, str(pr_number))
                    or pr_state
                )
                pr_state.status = "idle"
                pr_state.error_message = None
                await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
                self._status_cb(self._repo, pr_number, "idle", None)
                self._log_cb(f"PR #{pr_number} rebased and pushed ✓", "info")
            else:
                self._set_error(pr_state, pr_number, "Push rejected (force-with-lease failed)")
        else:
            self._set_error(pr_state, pr_number, "Rebase agent did not complete (check log with [v])")

    async def _do_ci_fix(
        self,
        pr_number: int,
        branch: str,
        worktree_path: Path,
        pr_state: PRState,
        log_path: Path,
        failures: str,
    ) -> None:
        pr_state.status = "fixing_ci"
        pr_state.error_message = None
        await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
        self._status_cb(self._repo, pr_number, "fixing_ci", None)

        old_sha = await git_get_current_sha(worktree_path)
        runner = AgentRunner(
            self._repo, pr_number, branch, worktree_path, self._state_manager, log_path
        )
        success = await runner.run_ci_fix(failures)

        if success:
            new_sha = await git_get_current_sha(worktree_path)
            if new_sha != old_sha:
                pushed = await git_push_force_with_lease(worktree_path, branch)
                if not pushed:
                    self._set_error(pr_state, pr_number, "Push rejected after CI fix")
                    return
                new_commits = await git_get_new_commits_since(worktree_path, old_sha)
                await self._state_manager.record_our_commits(
                    self._repo, str(pr_number), new_commits
                )
                self._log_cb(f"PR #{pr_number} CI fix pushed ✓", "info")
            else:
                self._log_cb(f"PR #{pr_number} CI fix: agent made no commits", "info")

            pr_state = (
                await self._state_manager.get_pr_state(self._repo, str(pr_number))
                or pr_state
            )
            pr_state.status = "idle"
            pr_state.error_message = None
            await self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
            self._status_cb(self._repo, pr_number, "idle", None)
        else:
            self._set_error(pr_state, pr_number, "CI fix agent did not complete (check log with [v])")

    def _set_error(self, pr_state: PRState, pr_number: int, msg: str) -> None:
        pr_state.status = "error"
        pr_state.error_message = msg
        asyncio.create_task(
            self._state_manager.upsert_pr_state(self._repo, str(pr_number), pr_state)
        )
        self._status_cb(self._repo, pr_number, "error", msg)
        self._log_cb(f"PR #{pr_number} error: {msg}", "error")


# ──────────────────────────────────────────────────────────────────────────────
# Display helpers
# ──────────────────────────────────────────────────────────────────────────────

def format_age(created_at_str: Optional[str]) -> str:
    if not created_at_str:
        return "?"
    try:
        created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        days = delta.days
        hours = delta.seconds // 3600
        return f"{days}d {hours}h" if days else f"{hours}h"
    except Exception:
        return "?"


async def build_display_list(
    repos: list[str], state_manager: StateManager
) -> list[PRDisplayInfo]:
    result: list[PRDisplayInfo] = []
    for repo in repos:
        for num_str, pr_state in (await state_manager.get_all_pr_states(repo)).items():
            try:
                number = int(num_str)
            except ValueError:
                continue
            result.append(PRDisplayInfo(
                repo=repo,
                number=number,
                title=pr_state.title or f"PR #{number}",
                branch=pr_state.branch or "",
                status=pr_state.status,
                age=format_age(pr_state.created_at),
                is_active=pr_state.status in ("rebasing", "fixing_ci"),
                error_message=pr_state.error_message,
            ))
    result.sort(key=lambda p: (p.repo, p.number))
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Textual messages (posted from background tasks → TUI)
# ──────────────────────────────────────────────────────────────────────────────

class PrStatusUpdate(Message):
    def __init__(self, repo: str, pr_number: int, status: str, error: Optional[str]) -> None:
        super().__init__()
        self.repo = repo
        self.pr_number = pr_number
        self.status = status
        self.error = error


class PrListUpdate(Message):
    def __init__(self, prs: list[PRDisplayInfo]) -> None:
        super().__init__()
        self.prs = prs


class AppLogMessage(Message):
    def __init__(self, text: str, level: str = "info") -> None:
        super().__init__()
        self.text = text
        self.level = level


# ──────────────────────────────────────────────────────────────────────────────
# Polling loop (runs as a background asyncio.Task)
# ──────────────────────────────────────────────────────────────────────────────

async def poll_loop(
    app: "PRManagerApp",
    state_manager: StateManager,
    poll_interval_minutes: int,
    recent_minutes: int,
) -> None:
    while True:
        try:
            repos = await state_manager.get_repos()
            if not repos:
                app.post_message(AppLogMessage(
                    "No repos configured — press [a] to add one.", "warn"
                ))
            else:
                app.post_message(AppLogMessage(f"Polling {len(repos)} repo(s)…", "info"))
                new_tasks: list[asyncio.Task] = []

                for repo in repos:
                    try:
                        prs = await gh_list_prs(repo)
                    except Exception as e:
                        app.post_message(AppLogMessage(
                            f"Failed to list PRs for {repo}: {e}", "error"
                        ))
                        continue

                    # Remove state + worktrees for PRs no longer in the list
                    # (covers other people's PRs fetched before --author @me,
                    # and PRs that have been closed/merged since last poll).
                    current_numbers = {str(p["number"]) for p in prs}
                    for old_num, _ in (await state_manager.get_all_pr_states(repo)).items():
                        if old_num not in current_numbers:
                            wt = get_worktree_path(repo, int(old_num))
                            if wt.exists():
                                await run_cmd(
                                    ["git", "worktree", "remove", "--force", str(wt)],
                                    cwd=get_repo_path(repo), check=False,
                                )
                            await state_manager.remove_pr(repo, old_num)
                            app.post_message(AppLogMessage(
                                f"Removed PR #{old_num} ({repo}) from state", "info"
                            ))

                    # Ensure all known PRs have at least stub state so they
                    # appear in the display before processing starts.
                    for pr_data in prs:
                        pn = str(pr_data["number"])
                        if await state_manager.get_pr_state(repo, pn) is None:
                            await state_manager.upsert_pr_state(repo, pn, PRState(
                                title=pr_data.get("title", ""),
                                branch=pr_data["headRefName"],
                                created_at=pr_data.get("createdAt", ""),
                            ))

                    # Push an updated display list now so PRs appear immediately.
                    _repos = await state_manager.get_repos()
                    app.post_message(PrListUpdate(await build_display_list(_repos, state_manager)))

                    for pr_data in prs:
                        pr_number: int = pr_data["number"]
                        key = (repo, pr_number)
                        existing = app._active_tasks.get(key)
                        if existing and not existing.done():
                            continue  # Already being processed.

                        # Capture loop variable to avoid late-binding closure issue.
                        _repo = repo

                        def make_status_cb() -> Callable[[str, int, str, Optional[str]], None]:
                            def cb(rr: str, nn: int, status: str, err: Optional[str]) -> None:
                                app.post_message(PrStatusUpdate(rr, nn, status, err))
                            return cb

                        def make_log_cb() -> Callable[[str, str], None]:
                            def cb(text: str, level: str) -> None:
                                app.post_message(AppLogMessage(text, level))
                            return cb

                        processor = PRProcessor(
                            repo=_repo,
                            pr_data=pr_data,
                            state_manager=state_manager,
                            status_cb=make_status_cb(),
                            log_cb=make_log_cb(),
                        )
                        task = asyncio.create_task(processor.process(recent_minutes))
                        app._active_tasks[key] = task
                        new_tasks.append(task)

                if new_tasks:
                    await asyncio.gather(*new_tasks, return_exceptions=True)

            # Refresh the full display list after each poll cycle.
            repos = await state_manager.get_repos()
            display = await build_display_list(repos, state_manager)
            app.post_message(PrListUpdate(display))

        except asyncio.CancelledError:
            return
        except Exception as e:
            app.post_message(AppLogMessage(f"Poll loop error: {e}", "error"))

        await asyncio.sleep(poll_interval_minutes * 60)


# ──────────────────────────────────────────────────────────────────────────────
# Add-repo modal
# ──────────────────────────────────────────────────────────────────────────────

class AddRepoScreen(ModalScreen):
    DEFAULT_CSS = """
    AddRepoScreen {
        align: center middle;
    }
    #dialog {
        padding: 1 2;
        width: 52;
        height: auto;
        border: thick $background 80%;
        background: $surface;
    }
    #dialog Label { margin-bottom: 1; }
    #dialog Input { margin-bottom: 1; }
    #buttons Button { margin-right: 1; }
    """

    def __init__(self, state_manager: StateManager) -> None:
        super().__init__()
        self._state_manager = state_manager

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Input(placeholder="owner/repo", id="repo-input")
            with Horizontal(id="buttons"):
                yield Button("Add", variant="primary", id="add-btn")
                yield Button("Cancel", id="cancel-btn")

    @on(Button.Pressed, "#add-btn")
    async def _add(self) -> None:
        repo = self.query_one("#repo-input", Input).value.strip()
        if "/" in repo:
            await self._state_manager.add_repo(repo)
            self.app.post_message(AppLogMessage(f"Added repo: {repo}", "info"))
            self.dismiss()
        else:
            self.app.post_message(AppLogMessage(
                "Invalid format — expected owner/repo", "error"
            ))

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()


# ──────────────────────────────────────────────────────────────────────────────
# Main TUI application
# ──────────────────────────────────────────────────────────────────────────────

class PRManagerApp(App):
    TITLE = "PR Manager"

    CSS = """
    DataTable {
        height: 1fr;
    }
    RichLog {
        height: 10;
        border-top: solid $primary-darken-2;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("o", "open_terminal", "terminal"),
        Binding("v", "view_agent", "view agent"),
        Binding("c", "open_claude_session", "claude session"),
        Binding("a", "add_repo", "add repo"),
        Binding("r", "remove_repo", "remove repo"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(
        self,
        state_manager: StateManager,
        poll_interval: int,
        recent_minutes: int,
    ) -> None:
        super().__init__()
        self._state_manager = state_manager
        self._poll_interval = poll_interval
        self._recent_minutes = recent_minutes
        self._display_prs: list[PRDisplayInfo] = []
        self._active_tasks: dict[tuple[str, int], asyncio.Task] = {}
        self._spinner_idx = 0
        self._poll_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable(id="pr-table", cursor_type="row")
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("PR#", "Repo", "Branch", "Status", "Age")
        self._poll_task = asyncio.create_task(
            poll_loop(
                self, self._state_manager, self._poll_interval, self._recent_minutes
            )
        )
        self.set_interval(0.12, self._tick_spinner)

    # ── Spinner ──────────────────────────────────────────────────────────────

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_CHARS)
        self._refresh_table()

    # ── Table rendering ───────────────────────────────────────────────────────

    def _format_status(self, status: str, is_active: bool) -> Text:
        icon, style = STATUS_STYLE.get(status, ("?", "dim"))
        if is_active:
            icon = SPINNER_CHARS[self._spinner_idx]
        label = status.replace("_", " ")
        return Text(f"{icon} {label}", style=style)

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
        saved_row = table.cursor_row
        table.clear()
        for pr in self._display_prs:
            table.add_row(
                str(pr.number),
                pr.repo,
                pr.branch,
                self._format_status(pr.status, pr.is_active),
                pr.age,
                key=f"{pr.repo}:{pr.number}",
            )
        # Restore cursor if possible.
        try:
            if self._display_prs:
                table.move_cursor(row=min(saved_row, len(self._display_prs) - 1))
        except Exception:
            pass

    # ── Message handlers ──────────────────────────────────────────────────────

    @on(PrListUpdate)
    def handle_pr_list_update(self, message: PrListUpdate) -> None:
        self._display_prs = message.prs
        self._refresh_table()

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
                    is_active=message.status in ("rebasing", "fixing_ci"),
                    error_message=message.error,
                )
                break
        self._refresh_table()

    @on(AppLogMessage)
    def handle_app_log_message(self, message: AppLogMessage) -> None:
        log = self.query_one(RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        color = {"info": "white", "error": "red", "warn": "yellow"}.get(message.level, "white")
        log.write(f"[dim]{ts}[/dim] [{color}]{message.text}[/]")

    # ── Selected PR helper ────────────────────────────────────────────────────

    def _get_selected_pr(self) -> Optional[PRDisplayInfo]:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._display_prs):
            return self._display_prs[row]
        return None

    # ── Key actions ───────────────────────────────────────────────────────────

    def _check_tmux(self) -> bool:
        if not os.environ.get("TMUX"):
            self.post_message(AppLogMessage(
                "Not inside a tmux session — run pr-manager inside tmux to use window actions",
                "warn",
            ))
            return False
        return True

    async def action_open_terminal(self) -> None:
        if not self._check_tmux():
            return
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        worktree = get_worktree_path(pr.repo, pr.number)
        if not worktree.exists():
            self.post_message(AppLogMessage(
                f"Worktree not yet created for PR #{pr.number} — try again after first poll", "warn"
            ))
            return
        await run_cmd(
            ["tmux", "new-window", "-c", str(worktree), "-n", f"pr-{pr.number}"],
            check=False,
        )
        self.post_message(AppLogMessage(f"Opened terminal for PR #{pr.number}", "info"))

    async def action_view_agent(self) -> None:
        if not self._check_tmux():
            return
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        log_path = get_log_path(pr.repo, pr.number)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        worktree = get_worktree_path(pr.repo, pr.number)
        cwd = str(worktree) if worktree.exists() else str(Path.home())
        await run_cmd([
            "tmux", "new-window",
            "-c", cwd,
            "-n", f"log-{pr.number}",
            f"tail -f {log_path}",
        ], check=False)
        self.post_message(AppLogMessage(
            f"Watching agent log for PR #{pr.number} (tail -f {log_path})", "info"
        ))

    async def action_open_claude_session(self) -> None:
        if not self._check_tmux():
            return
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        # Interrupt the running automated agent task if any.
        key = (pr.repo, pr.number)
        task = self._active_tasks.get(key)
        if task and not task.done():
            task.cancel()
            self.post_message(AppLogMessage(
                f"Interrupted automated agent for PR #{pr.number}", "warn"
            ))
        worktree = get_worktree_path(pr.repo, pr.number)
        cwd = str(worktree) if worktree.exists() else str(Path.home())
        pr_state = await self._state_manager.get_pr_state(pr.repo, str(pr.number))
        if pr_state and pr_state.session_id:
            cmd = f"claude --resume {pr_state.session_id}"
        else:
            cmd = "claude"
        await run_cmd([
            "tmux", "new-window",
            "-c", cwd,
            "-n", f"claude-{pr.number}",
            cmd,
        ], check=False)
        self.post_message(AppLogMessage(
            f"Opened Claude session for PR #{pr.number}", "info"
        ))

    async def action_add_repo(self) -> None:
        await self.push_screen(AddRepoScreen(self._state_manager))

    async def action_remove_repo(self) -> None:
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected — select a row first", "warn"))
            return
        await self._state_manager.remove_repo(pr.repo)
        self.post_message(AppLogMessage(f"Removed repo: {pr.repo}", "info"))

    async def on_unmount(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()


# ──────────────────────────────────────────────────────────────────────────────
# CLI entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pr-manager",
        description="GitHub PR auto-manager with Claude agent integration",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Start the TUI manager")
    run_p.add_argument(
        "--recent-minutes", type=int, default=30, metavar="N",
        help="Ignore PRs with human commits within the last N minutes (default: 30)",
    )
    run_p.add_argument(
        "--poll-interval", type=int, default=5, metavar="N",
        help="Polling interval in minutes (default: 5)",
    )

    add_p = sub.add_parser("add", help="Add a repo to manage")
    add_p.add_argument("repo", help="owner/repo")

    rem_p = sub.add_parser("remove", help="Stop managing a repo")
    rem_p.add_argument("repo", help="owner/repo")

    sub.add_parser("list", help="List all managed repos")

    args = parser.parse_args()
    state_manager = StateManager()

    if args.command == "run":
        asyncio.run(state_manager.load())
        PRManagerApp(state_manager, args.poll_interval, args.recent_minutes).run()

    elif args.command == "add":
        async def _add() -> None:
            await state_manager.load()
            await state_manager.add_repo(args.repo)
            print(f"Added {args.repo}")
        asyncio.run(_add())

    elif args.command == "remove":
        async def _remove() -> None:
            await state_manager.load()
            await state_manager.remove_repo(args.repo)
            print(f"Removed {args.repo}")
        asyncio.run(_remove())

    elif args.command == "list":
        async def _list() -> None:
            await state_manager.load()
            repos = await state_manager.get_repos()
            if repos:
                for r in repos:
                    print(r)
            else:
                print("No repos configured. Use: pr-manager add owner/repo")
        asyncio.run(_list())


if __name__ == "__main__":
    main()
