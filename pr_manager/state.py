from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterator, Optional, TypeVar

from .constants import STATE_PATH

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class PRState:
    session_id: Optional[str] = None
    our_commits: list[str] = field(default_factory=list)
    status: str = "idle"
    # User chose to hide this (still-open) PR from the listing.
    hidden: bool = False
    last_checked: Optional[str] = None
    error_message: Optional[str] = None
    title: str = ""
    branch: str = ""
    created_at: Optional[str] = None
    is_draft: bool = False
    review_decision: str = ""
    comment_count: int = 0
    review_count: int = 0
    latest_activity: Optional[str] = None


CLAUDE_PERMISSION_MODES = ["default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto"]


@dataclass
class Settings:
    claude_permission_mode: str = "default"
    theme: str = "textual-light"


_SETTINGS_FIELDS = set(Settings.__dataclass_fields__)


def _dict_to_settings(d: dict) -> Settings:
    from textual.theme import BUILTIN_THEMES
    s = Settings(**{k: v for k, v in d.items() if k in _SETTINGS_FIELDS})
    if s.claude_permission_mode not in CLAUDE_PERMISSION_MODES:
        s.claude_permission_mode = "default"
    if s.theme not in BUILTIN_THEMES:
        s.theme = Settings.theme
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
    error_message: Optional[str]
    review_status: str = ""
    activity: str = ""
    hidden: bool = False


_PR_STATE_FIELDS = set(PRState.__dataclass_fields__)


def _dict_to_pr_state(d: dict) -> PRState:
    return PRState(**{k: v for k, v in d.items() if k in _PR_STATE_FIELDS})


class StateManager:
    """Persistent app state backed by state.json.

    Cross-process safety: `pr-manager run` (TUI) and `pr-manager fix`
    hold separate StateManagers over the same file, concurrently.  Every
    operation is therefore a fresh read-modify-write of the file under an
    exclusive OS file lock — one process's writes are neither clobbered
    by nor invisible to the other.  (Multi-call sequences such as a
    get/upsert pair are still not transactional; single operations are.)
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = AppState()
        self._nudge: Optional[asyncio.Event] = None

    def set_nudge(self, event: asyncio.Event) -> None:
        """Register an event to be set whenever the tracked repo list
        changes, so a running poll loop can wake up immediately."""
        self._nudge = event

    async def load(self) -> None:
        await self._transact(lambda st: None, write=False)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_path = STATE_PATH.with_suffix(".lock")
        with open(lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _reload_sync(self) -> None:
        """Re-read state.json. Must be called under the file lock.

        A corrupt file is set aside (state.json.corrupt-<ts>) and we
        start fresh rather than crashing every command at startup.
        """
        if not STATE_PATH.exists():
            self._state = AppState()
            return
        try:
            data = json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError) as e:
            backup = STATE_PATH.with_name(
                f"{STATE_PATH.name}.corrupt-{int(time.time())}"
            )
            os.replace(STATE_PATH, backup)
            log.error(
                "State file %s is corrupt (%s) — moved to %s, starting fresh",
                STATE_PATH, e, backup,
            )
            self._state = AppState()
            return
        # Legacy keys (`disabled_prs`, `hidden_prs`, …) are ignored
        # silently for forward-compat with old state files.
        self._state = AppState(
            repos=data.get("repos", []),
            pr_state=data.get("pr_state", {}),
            local_branches=data.get("local_branches", {}),
            settings=_dict_to_settings(data.get("settings", {})),
        )

    def _save_sync(self) -> None:
        """Write state to disk atomically. Must be called under the file lock."""
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(f".tmp{os.getpid()}")
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

    def _transact_sync(self, fn: Callable[[AppState], T], write: bool) -> T:
        with self._file_lock():
            self._reload_sync()
            result = fn(self._state)
            if write:
                self._save_sync()
            return result

    async def _transact(self, fn: Callable[[AppState], T], *, write: bool) -> T:
        """Run `fn` against freshly loaded state under the file lock,
        saving afterwards when `write` is set.

        Runs in a thread: the flock wait is uninterruptible, so if
        another process wedged while holding the lock, taking it on the
        event loop would freeze the whole TUI.
        """
        async with self._lock:
            return await asyncio.to_thread(self._transact_sync, fn, write)

    async def add_repo(self, repo: str) -> None:
        def mutate(st: AppState) -> None:
            if repo not in st.repos:
                st.repos.append(repo)

        await self._transact(mutate, write=True)
        if self._nudge is not None:
            self._nudge.set()

    async def remove_repo(self, repo: str) -> None:
        def mutate(st: AppState) -> None:
            st.repos = [r for r in st.repos if r != repo]
            st.pr_state.pop(repo, None)

        await self._transact(mutate, write=True)

    async def get_repos(self) -> list[str]:
        return await self._transact(lambda st: list(st.repos), write=False)

    async def get_pr_state(self, repo: str, pr_number: str) -> Optional[PRState]:
        def read(st: AppState) -> Optional[PRState]:
            d = st.pr_state.get(repo, {}).get(str(pr_number))
            return _dict_to_pr_state(d) if d is not None else None

        return await self._transact(read, write=False)

    async def get_all_pr_states(self, repo: str) -> dict[str, PRState]:
        def read(st: AppState) -> dict[str, PRState]:
            return {
                num: _dict_to_pr_state(d)
                for num, d in st.pr_state.get(repo, {}).items()
            }

        return await self._transact(read, write=False)

    async def upsert_pr_state(self, repo: str, pr_number: str, state: PRState) -> None:
        def mutate(st: AppState) -> None:
            st.pr_state.setdefault(repo, {})[str(pr_number)] = asdict(state)

        await self._transact(mutate, write=True)

    async def record_our_commits(self, repo: str, pr_number: str, shas: list[str]) -> None:
        def mutate(st: AppState) -> None:
            repo_map = st.pr_state.setdefault(repo, {})
            pr_dict = repo_map.setdefault(str(pr_number), {})
            existing = set(pr_dict.get("our_commits", []))
            existing.update(shas)
            pr_dict["our_commits"] = list(existing)

        await self._transact(mutate, write=True)

    async def set_pr_hidden(self, repo: str, pr_number: str, hidden: bool) -> None:
        def mutate(st: AppState) -> None:
            pr = st.pr_state.get(repo, {}).get(str(pr_number))
            if pr is not None:
                pr["hidden"] = hidden

        await self._transact(mutate, write=True)

    async def remove_pr(self, repo: str, pr_number: str) -> None:
        def mutate(st: AppState) -> None:
            st.pr_state.get(repo, {}).pop(str(pr_number), None)

        await self._transact(mutate, write=True)

    async def add_local_branch(self, repo: str, branch: str) -> None:
        def mutate(st: AppState) -> None:
            branches = st.local_branches.setdefault(repo, [])
            if branch not in branches:
                branches.append(branch)

        await self._transact(mutate, write=True)

    async def remove_local_branch(self, repo: str, branch: str) -> None:
        def mutate(st: AppState) -> None:
            branches = st.local_branches.get(repo, [])
            if branch in branches:
                branches.remove(branch)

        await self._transact(mutate, write=True)

    async def get_local_branches(self, repo: str) -> list[str]:
        return await self._transact(
            lambda st: list(st.local_branches.get(repo, [])), write=False,
        )

    async def get_all_local_branches(self) -> dict[str, list[str]]:
        return await self._transact(
            lambda st: {r: list(bs) for r, bs in st.local_branches.items()},
            write=False,
        )

    async def get_settings(self) -> Settings:
        return await self._transact(
            lambda st: Settings(**asdict(st.settings)), write=False,
        )

    async def update_settings(self, settings: Settings) -> None:
        def mutate(st: AppState) -> None:
            st.settings = settings

        await self._transact(mutate, write=True)
