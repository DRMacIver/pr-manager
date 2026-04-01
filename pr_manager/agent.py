from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .state import PRState, StateManager


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class AgentLogger:
    """Writes to a log file with explicit flush after every write."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)

    def write(self, line: str) -> None:
        os.write(self._fd, (line + "\n").encode())

    def close(self) -> None:
        os.close(self._fd)


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

        options = ClaudeAgentOptions(
            cwd=str(self._worktree_path),
            allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            resume=session_id,
            max_turns=50,
        )

        log = AgentLogger(self._log_path)
        found_done = False
        try:
            log.write(f"[{_ts()}] === Agent started (session={session_id or 'new'}, cwd={self._worktree_path}) ===")
            log.write(f"[{_ts()}] Prompt: {prompt[:200]}...")

            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage):
                    log.write(f"[{_ts()}] [SYS] subtype={message.subtype} data={json.dumps(message.data, default=str)[:300]}")
                    if message.subtype == "init":
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
                            for line in block.text.splitlines():
                                log.write(f"[{_ts()}] {line}")
                        elif isinstance(block, ToolUseBlock):
                            log.write(f"[{_ts()}] >>> {block.name}(id={block.id})")
                            input_str = json.dumps(block.input, default=str)
                            # Log full input for short inputs, truncated for long
                            if len(input_str) > 500:
                                log.write(f"[{_ts()}]     input: {input_str[:500]}...")
                            else:
                                log.write(f"[{_ts()}]     input: {input_str}")
                        else:
                            log.write(f"[{_ts()}] [BLOCK] {type(block).__name__}: {str(block)[:300]}")

                elif isinstance(message, UserMessage):
                    for block in (message.content if isinstance(message.content, list) else []):
                        if isinstance(block, ToolResultBlock):
                            err = " [ERROR]" if block.is_error else ""
                            content = str(block.content) if block.content else ""
                            if len(content) > 2000:
                                # Log first and last portion for long outputs
                                log.write(f"[{_ts()}] <<< ({len(content)} chars){err}")
                                for line in content[:1000].splitlines():
                                    log.write(f"[{_ts()}] <<< {line}")
                                log.write(f"[{_ts()}] <<< ... ({len(content) - 2000} chars omitted) ...")
                                for line in content[-1000:].splitlines():
                                    log.write(f"[{_ts()}] <<< {line}")
                            else:
                                for line in content.splitlines():
                                    log.write(f"[{_ts()}] <<< {line}{err}")
                        elif isinstance(block, ToolUseBlock):
                            log.write(f"[{_ts()}] [USER-TOOL] {block.name}(id={block.id})")
                        else:
                            log.write(f"[{_ts()}] [USER-BLOCK] {type(block).__name__}: {str(block)[:300]}")

                elif isinstance(message, ResultMessage):
                    found_done = bool(message.result and "DONE" in message.result.upper())
                    log.write(f"[{_ts()}] === Agent finished (DONE={found_done}) ===")
                    if message.result:
                        log.write(f"[{_ts()}] Result: {message.result[:500]}")

                elif isinstance(message, RateLimitEvent):
                    info = message.rate_limit_info
                    log.write(f"[{_ts()}] [RATE] status={info.status} type={info.rate_limit_type} util={info.utilization}")

                elif isinstance(message, StreamEvent):
                    event = message.event
                    log.write(f"[{_ts()}] [STREAM] {json.dumps(event, default=str)[:300]}")

                else:
                    log.write(f"[{_ts()}] [???] {type(message).__name__}: {str(message)[:300]}")

        except (CLINotFoundError, CLIConnectionError) as e:
            log.write(f"[{_ts()}] [FATAL] Agent SDK error: {e}")
            return False
        except asyncio.CancelledError:
            log.write(f"[{_ts()}] [INFO] Agent cancelled by user")
            raise
        except Exception as e:
            log.write(f"[{_ts()}] [FATAL] Unexpected error: {type(e).__name__}: {e}")
            return False
        finally:
            log.close()

        return found_done
