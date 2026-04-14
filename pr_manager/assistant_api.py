from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .git import get_log_path

if TYPE_CHECKING:
    from .state import StateManager
    from .tui import PRManagerApp


class AssistantContext:
    """Python API available to the assistant's executed code as ``ctx``."""

    def __init__(
        self,
        app: PRManagerApp,
        state_manager: StateManager,
        active_tasks: dict[tuple[str, int], asyncio.Task[Any]],
    ) -> None:
        self._app = app
        self._state_manager = state_manager
        self._active_tasks = active_tasks

    # ── State inspection ────────────────────────────────────────────────

    async def list_repos(self) -> list[str]:
        """List all tracked repositories."""
        return await self._state_manager.get_repos()

    async def get_pr(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        """Get the full state of a PR as a dict."""
        st = await self._state_manager.get_pr_state(repo, str(pr_number))
        if st is None:
            return None
        return asdict(st)

    async def list_prs(self, repo: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        """List all PRs and their states. Optionally filter by repo."""
        repos = [repo] if repo else await self._state_manager.get_repos()
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for r in repos:
            states = await self._state_manager.get_all_pr_states(r)
            result[r] = {num: asdict(st) for num, st in states.items()}
        return result

    def get_display_prs(self) -> list[dict[str, Any]]:
        """Get the current table display data."""
        return [
            {
                "repo": pr.repo,
                "number": pr.number,
                "title": pr.title,
                "branch": pr.branch,
                "status": pr.status,
                "age": pr.age,
                "is_active": pr.is_active,
                "error_message": pr.error_message,
                "review_status": pr.review_status,
                "activity": pr.activity,
            }
            for pr in self._app._display_prs
        ]

    # ── Agent inspection ────────────────────────────────────────────────

    def list_running_agents(self) -> list[dict[str, Any]]:
        """List all currently running automated agent tasks."""
        return [
            {
                "repo": repo,
                "pr_number": pr_num,
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            for (repo, pr_num), task in self._active_tasks.items()
        ]

    def read_agent_log(self, repo: str, pr_number: int, tail: int = 50) -> str:
        """Read the last N lines of an agent's log file."""
        log_path = get_log_path(repo, pr_number)
        if not log_path.exists():
            return "(no log file)"
        lines = log_path.read_text().splitlines()
        if len(lines) > tail:
            lines = lines[-tail:]
        return "\n".join(lines)

    # ── Agent control ───────────────────────────────────────────────────

    def cancel_agent(self, repo: str, pr_number: int) -> bool:
        """Cancel a running automated agent task. Returns True if cancelled."""
        key = (repo, pr_number)
        task = self._active_tasks.get(key)
        if task and not task.done():
            task.cancel()
            return True
        return False

    # ── UI control ──────────────────────────────────────────────────────

    def log(self, message: str, level: str = "info") -> None:
        """Write a message to the app's log panel."""
        from .tui import AppLogMessage

        self._app.post_message(AppLogMessage(message, level))

    # ── State modification ──────────────────────────────────────────────

    async def set_pr_status(
        self, repo: str, pr_number: int, status: str, error: str | None = None,
    ) -> None:
        """Update a PR's status and optionally set an error message."""
        from .tui import PrStatusUpdate

        st = await self._state_manager.get_pr_state(repo, str(pr_number))
        if st is None:
            return
        st.status = status
        st.error_message = error
        await self._state_manager.upsert_pr_state(repo, str(pr_number), st)
        self._app.post_message(PrStatusUpdate(repo, pr_number, status, error))

    async def add_repo(self, repo: str) -> None:
        """Add a repository to track."""
        await self._state_manager.add_repo(repo)

    async def remove_repo(self, repo: str) -> None:
        """Remove a repository from tracking."""
        await self._state_manager.remove_repo(repo)

    async def hide_pr(self, repo: str, pr_number: int) -> None:
        """Hide a PR from the displayed list (persists across restarts).
        Local clones are left intact."""
        task = self._active_tasks.pop((repo, pr_number), None)
        if task and not task.done():
            task.cancel()
        await self._state_manager.hide_pr(repo, pr_number)

    async def unhide_pr(self, repo: str, pr_number: int) -> None:
        """Restore a previously hidden PR to the list."""
        await self._state_manager.unhide_pr(repo, pr_number)
