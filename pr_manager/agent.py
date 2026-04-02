"""Agent runner — executes Claude Code inside Docker containers via docker exec."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

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
        container_name: str,
        state_manager: StateManager,
        log_path: Path,
    ) -> None:
        self._repo = repo
        self._pr_number = pr_number
        self._branch = branch
        self._container_name = container_name
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

        # Build claude CLI command to run inside the container.
        claude_args = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", "50",
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Bash,Read,Write,Edit,Glob,Grep",
            "--bare",
        ]
        if session_id:
            claude_args += ["--resume", session_id]
        claude_args.append(prompt)

        docker_cmd = [
            "docker", "exec",
            "-w", "/home/dev/repo",
            self._container_name,
        ] + claude_args

        log = AgentLogger(self._log_path)
        found_done = False
        try:
            log.write(f"[{_ts()}] === Agent started (container={self._container_name}, session={session_id or 'new'}) ===")
            log.write(f"[{_ts()}] Prompt: {prompt[:200]}...")

            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            assert proc.stdout is not None
            assert proc.stderr is not None
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.write(f"[{_ts()}] [RAW] {line[:500]}")
                    continue

                event_type = event.get("type", "")

                if event_type == "system":
                    subtype = event.get("subtype", "")
                    log.write(f"[{_ts()}] [SYS] subtype={subtype}")
                    if subtype == "init":
                        new_sid = event.get("session_id")
                        if new_sid:
                            current = await self._state_manager.get_pr_state(
                                self._repo, str(self._pr_number)
                            ) or PRState()
                            current.session_id = new_sid
                            await self._state_manager.upsert_pr_state(
                                self._repo, str(self._pr_number), current
                            )

                elif event_type == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        block_type = block.get("type", "")
                        if block_type == "text":
                            for text_line in block.get("text", "").splitlines():
                                log.write(f"[{_ts()}] {text_line}")
                        elif block_type == "tool_use":
                            name = block.get("name", "?")
                            tool_input = json.dumps(block.get("input", {}), default=str)
                            if len(tool_input) > 500:
                                log.write(f"[{_ts()}] >>> {name}(id={block.get('id', '?')})")
                                log.write(f"[{_ts()}]     input: {tool_input[:500]}...")
                            else:
                                log.write(f"[{_ts()}] >>> {name}: {tool_input}")
                        elif block_type == "tool_result":
                            content = str(block.get("content", ""))
                            err = " [ERROR]" if block.get("is_error") else ""
                            if len(content) > 2000:
                                log.write(f"[{_ts()}] <<< ({len(content)} chars){err}")
                                for cl in content[:1000].splitlines():
                                    log.write(f"[{_ts()}] <<< {cl}")
                                log.write(f"[{_ts()}] <<< ... ({len(content) - 2000} chars omitted) ...")
                                for cl in content[-1000:].splitlines():
                                    log.write(f"[{_ts()}] <<< {cl}")
                            else:
                                for cl in content.splitlines():
                                    log.write(f"[{_ts()}] <<< {cl}{err}")
                        else:
                            log.write(f"[{_ts()}] [BLOCK] {block_type}: {json.dumps(block, default=str)[:300]}")

                elif event_type == "result":
                    result_text = event.get("result", "")
                    found_done = bool(result_text and "DONE" in result_text.upper())
                    is_error = event.get("is_error", False)
                    log.write(f"[{_ts()}] === Agent finished (DONE={found_done}, error={is_error}) ===")
                    if result_text:
                        log.write(f"[{_ts()}] Result: {result_text[:500]}")

                elif event_type == "rate_limit_event":
                    info = event.get("rate_limit_info", {})
                    log.write(f"[{_ts()}] [RATE] status={info.get('status')} type={info.get('rateLimitType')}")

                else:
                    log.write(f"[{_ts()}] [???] {event_type}: {json.dumps(event, default=str)[:300]}")

            await proc.wait()
            if proc.returncode != 0:
                stderr = (await proc.stderr.read()).decode(errors="replace").strip()
                log.write(f"[{_ts()}] [FATAL] docker exec exited with code {proc.returncode}: {stderr}")
                if not found_done:
                    return False

        except asyncio.CancelledError:
            log.write(f"[{_ts()}] [INFO] Agent cancelled")
            try:
                proc.terminate()
            except (NameError, ProcessLookupError):
                pass
            raise
        except Exception as e:
            log.write(f"[{_ts()}] [FATAL] {type(e).__name__}: {e}")
            return False
        finally:
            log.close()

        return found_done
