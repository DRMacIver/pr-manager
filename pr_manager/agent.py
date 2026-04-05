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

    async def run_rebase(self, target_branch: str = "main") -> str | None:
        prompt = (
            f"PR #{self._pr_number} (branch: {self._branch}) needs to be rebased on top of {target_branch}.\n"
            "Steps:\n"
            "1. Run: git fetch origin\n"
            f"2. Run: git rebase origin/{target_branch}\n"
            "3. Resolve any conflicts if they arise\n"
            "4. Once the rebase has succeeded, output exactly: DONE"
        )
        return await self._run_agent(prompt)

    async def run_ci_fix(self, failures: str) -> str | None:
        prompt = (
            f"PR #{self._pr_number} (branch: {self._branch}) has failing CI checks:\n"
            f"{failures}\n\n"
            "IMPORTANT: Before attempting any fix, triage the failures:\n"
            "1. Run: git diff --name-only origin/main...HEAD\n"
            "2. Compare the files this PR modifies with the failing tests/errors.\n"
            "3. If the failures are clearly unrelated to the PR's changes (e.g. upstream\n"
            "   dependency breakage, infrastructure issues, flaky tests in untouched code),\n"
            "   do NOT attempt to fix them. Instead output exactly: UNFIXABLE\n\n"
            "Only if the failures are plausibly caused by this PR's changes, fix the code.\n"
            "After fixing, verify your changes locally before committing:\n"
            "- Run the failing tests/checks locally if possible\n"
            "- If local verification passes, commit (use git add -A && git commit)\n"
            "- If local verification fails, keep iterating until the tests pass\n"
            "When complete, output exactly: DONE"
        )
        return await self._run_agent(prompt)

    async def run_ci_fix_review(
        self, fix_response: str, failures: str, pr_title: str,
    ) -> tuple[str, str]:
        """Review an UNFIXABLE claim. Returns ("accept"|"reject", feedback)."""
        prompt = (
            "You are reviewing whether a CI fix agent's refusal to fix CI failures is justified.\n\n"
            f"PR #{self._pr_number} (branch: {self._branch})\n"
            f"PR title: {pr_title}\n\n"
            f"Failing CI checks:\n{failures}\n\n"
            f"The CI fix agent was asked to fix these failures but responded:\n"
            f"---\n{fix_response}\n---\n\n"
            "Your job: determine if this refusal is legitimate.\n\n"
            "A refusal is LEGITIMATE only if the failures are genuinely unrelated to the PR's\n"
            "purpose AND the PR was not created to fix them. For example, a docs typo PR\n"
            "should not be expected to fix unrelated test failures on main.\n\n"
            "A refusal is NOT LEGITIMATE if:\n"
            "- The PR's stated purpose includes fixing these failures\n"
            "- The agent is merely noting that failures pre-existed on main, rather than\n"
            "  explaining why they genuinely cannot be fixed\n"
            "- The agent is treating explicitly requested work as 'out of scope'\n\n"
            "Check the PR's title, description, and commit history to understand its purpose.\n"
            f"Run: gh pr view {self._pr_number} --repo {self._repo}\n\n"
            "Output EXACTLY one of:\n"
            "- ACCEPT (if the refusal is legitimate — the failures truly aren't this PR's job)\n"
            "- REJECT: followed by a critique explaining why the agent must fix these failures"
        )
        result = await self._run_agent(
            prompt, persist_session=False, max_turns=15,
        )
        result_str = result or ""
        if "REJECT" in result_str.upper():
            idx = result_str.upper().find("REJECT")
            feedback = result_str[idx + len("REJECT"):].lstrip(": ")
            return "reject", feedback or result_str
        return "accept", result_str

    async def run_ci_fix_retry(self, review_feedback: str) -> str | None:
        """Resume the CI fix agent session with reviewer feedback."""
        prompt = (
            "A reviewer has examined your UNFIXABLE claim and rejected it:\n\n"
            f"{review_feedback}\n\n"
            "You MUST fix the failing CI checks. The UNFIXABLE response is not acceptable.\n"
            "Fix the code, then verify your changes locally by running the failing tests/checks.\n"
            "Keep iterating until local verification passes.\n"
            "Commit your changes (use git add -A && git commit).\n"
            "When complete, output exactly: DONE"
        )
        return await self._run_agent(prompt)

    async def _run_agent(
        self, prompt: str, *, persist_session: bool = True, max_turns: int = 50,
    ) -> str | None:
        if persist_session:
            pr_state = await self._state_manager.get_pr_state(self._repo, str(self._pr_number))
            session_id = pr_state.session_id if pr_state else None
        else:
            session_id = None

        options = ClaudeAgentOptions(
            cwd=str(self._worktree_path),
            allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            resume=session_id,
            max_turns=max_turns,
        )

        log = AgentLogger(self._log_path)
        result_text: str | None = None
        try:
            log.write(f"[{_ts()}] === Agent started (session={session_id or 'new'}, cwd={self._worktree_path}) ===")
            log.write(f"[{_ts()}] Prompt: {prompt[:200]}...")

            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage):
                    log.write(f"[{_ts()}] [SYS] subtype={message.subtype} data={json.dumps(message.data, default=str)[:300]}")
                    if persist_session and message.subtype == "init":
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
                    result_text = message.result
                    log.write(f"[{_ts()}] === Agent finished ===")
                    if result_text:
                        log.write(f"[{_ts()}] Result: {result_text[:500]}")
                    break

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
            return None
        except asyncio.CancelledError:
            log.write(f"[{_ts()}] [INFO] Agent cancelled by user")
            raise
        except Exception as e:
            log.write(f"[{_ts()}] [FATAL] Unexpected error: {type(e).__name__}: {e}")
            return None
        finally:
            log.close()

        return result_text
