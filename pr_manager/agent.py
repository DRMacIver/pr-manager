from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from pathlib import Path

from .state import PRState, StateManager


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
                            ts = datetime.now().strftime("%H:%M:%S")
                            if isinstance(block, TextBlock):
                                for line in block.text.splitlines():
                                    log_f.write(f"[{ts}] {line}\n")
                            elif isinstance(block, ToolUseBlock):
                                log_f.write(f"[{ts}] >>> {block.name}({json.dumps(block.input, default=str)[:200]})\n")

                    elif isinstance(message, UserMessage):
                        for block in (message.content if isinstance(message.content, list) else []):
                            if isinstance(block, ToolResultBlock):
                                ts = datetime.now().strftime("%H:%M:%S")
                                err = " [ERROR]" if block.is_error else ""
                                content = str(block.content)[:500] if block.content else ""
                                for line in content.splitlines():
                                    log_f.write(f"[{ts}] <<< {line}{err}\n")

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
