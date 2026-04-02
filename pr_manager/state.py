from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from .constants import STATE_PATH


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


CLAUDE_PERMISSION_MODES = ["default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto"]


@dataclass
class Settings:
    claude_permission_mode: str = "default"


_SETTINGS_FIELDS = set(Settings.__dataclass_fields__)


def _dict_to_settings(d: dict) -> Settings:
    s = Settings(**{k: v for k, v in d.items() if k in _SETTINGS_FIELDS})
    if s.claude_permission_mode not in CLAUDE_PERMISSION_MODES:
        s.claude_permission_mode = "default"
    return s


@dataclass
class AppState:
    repos: list[str] = field(default_factory=list)
    pr_state: dict[str, dict[str, dict]] = field(default_factory=dict)
    # repo -> list of branch names that don't have PRs yet
    local_branches: dict[str, list[str]] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)


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
                    local_branches=data.get("local_branches", {}),
                    settings=_dict_to_settings(data.get("settings", {})),
                )
            else:
                self._state = AppState()

    def _save_sync(self) -> None:
        """Write state to disk atomically. Must be called while holding self._lock."""
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {
                "repos": self._state.repos,
                "pr_state": self._state.pr_state,
                "local_branches": self._state.local_branches,
                "settings": asdict(self._state.settings),
            },
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

    async def add_local_branch(self, repo: str, branch: str) -> None:
        async with self._lock:
            branches = self._state.local_branches.setdefault(repo, [])
            if branch not in branches:
                branches.append(branch)
                self._save_sync()

    async def remove_local_branch(self, repo: str, branch: str) -> None:
        async with self._lock:
            branches = self._state.local_branches.get(repo, [])
            if branch in branches:
                branches.remove(branch)
                self._save_sync()

    async def get_local_branches(self, repo: str) -> list[str]:
        async with self._lock:
            return list(self._state.local_branches.get(repo, []))

    async def get_all_local_branches(self) -> dict[str, list[str]]:
        async with self._lock:
            return {r: list(bs) for r, bs in self._state.local_branches.items()}

    async def get_settings(self) -> Settings:
        async with self._lock:
            return Settings(**asdict(self._state.settings))

    async def update_settings(self, settings: Settings) -> None:
        async with self._lock:
            self._state.settings = settings
            self._save_sync()
