from __future__ import annotations

import ast
import io
import textwrap
import traceback
from typing import Any, Callable

import anthropic
from anthropic.types import ToolParam

from .assistant_api import AssistantContext

SYSTEM_PROMPT = """\
You are an interactive assistant embedded in a PR manager TUI. You help the user \
inspect and control their PR management system.

You have one tool: `execute_python`, which runs Python code in the TUI's process. \
An `ctx` object is available in your code with the API described below.

All async methods support `await` — your code runs inside an async function. \
The return value of the last expression is automatically captured.

## ctx API

### State inspection
- `await ctx.list_repos()` → list[str] — tracked repositories
- `await ctx.list_prs(repo=None)` → dict — all PR states (nested: repo → pr_number → state dict)
- `await ctx.get_pr(repo, pr_number)` → dict | None — full state dict for one PR
- `ctx.get_display_prs()` → list[dict] — current table rows with keys: repo, number, title, \
branch, status, age, is_active, error_message, review_status, activity

### Agent inspection
- `ctx.list_running_agents()` → list[dict] — running agent tasks (repo, pr_number, done, cancelled)
- `ctx.read_agent_log(repo, pr_number, tail=50)` → str — last N log lines for an agent

### Agent control
- `ctx.cancel_agent(repo, pr_number)` → bool — cancel a running agent, returns True if cancelled

### UI control
- `ctx.log(message, level="info")` — write to the TUI's log panel (levels: info, warn, error)

### State modification
- `await ctx.set_pr_status(repo, pr_number, status, error=None)` — update a PR's status
- `await ctx.add_repo(repo)` / `await ctx.remove_repo(repo)` — manage tracked repos

### Advanced access
- `ctx._app` — the Textual PRManagerApp instance
- `ctx._state_manager` — the StateManager instance
- `ctx._active_tasks` — dict of (repo, pr_number) → asyncio.Task

You can `import` any module. subprocess calls, file I/O, and network requests are all available.

Use `print()` to produce output (captured automatically). Keep responses concise.
"""


TOOLS: list[ToolParam] = [
    {
        "name": "execute_python",
        "description": (
            "Execute Python code in the PR manager process. "
            "`ctx` is available for inspecting/controlling the TUI. "
            "Supports `await`. Use `print()` for output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute.",
                },
            },
            "required": ["code"],
        },
    },
]


class Assistant:
    """Interactive AI assistant that runs Python code in-process."""

    def __init__(
        self, ctx: AssistantContext, model: str = "claude-sonnet-4-20250514",
    ) -> None:
        self.ctx = ctx
        self.model = model
        self.client = anthropic.AsyncAnthropic()
        self.messages: list[Any] = []

    async def send(
        self,
        user_text: str,
        on_tool_use: Callable[[str], None] | None = None,
    ) -> str:
        """Send a user message and return the assistant's final text response."""
        self.messages.append({"role": "user", "content": user_text})

        while True:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.messages,
            )

            self.messages.append({
                "role": "assistant",
                "content": [block.model_dump() for block in response.content],
            })

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return "".join(
                    b.text for b in response.content if b.type == "text"
                )

            tool_results = []
            for tu in tool_uses:
                input_data: dict[str, Any] = tu.input  # type: ignore[assignment]
                code: str = input_data.get("code", "")
                if on_tool_use:
                    on_tool_use(code)
                result = await self._exec_python(code)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })
            self.messages.append({"role": "user", "content": tool_results})

    async def _exec_python(self, code: str) -> str:
        """Execute Python code with ``ctx`` available, capturing output."""
        stdout = io.StringIO()

        def _print(*args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("file", stdout)
            print(*args, **kwargs)

        namespace: dict[str, Any] = {"ctx": self.ctx, "print": _print}

        try:
            indented = textwrap.indent(code, "    ")
            wrapped = f"async def __exec__():\n{indented}"
            tree = ast.parse(wrapped)
            func_def = tree.body[0]
            assert isinstance(func_def, ast.AsyncFunctionDef)
            # Auto-return the value of the last expression statement.
            if func_def.body and isinstance(func_def.body[-1], ast.Expr):
                last_expr = func_def.body[-1]
                func_def.body[-1] = ast.Return(value=last_expr.value)
                ast.fix_missing_locations(tree)
            compiled = compile(tree, "<assistant>", "exec")
            exec(compiled, namespace)
            result = await namespace["__exec__"]()
            output = stdout.getvalue()
            if result is not None:
                if output:
                    output += "\n"
                output += repr(result)
            return output or "(no output)"
        except Exception:
            output = stdout.getvalue()
            tb = traceback.format_exc()
            if output:
                return output + "\n" + tb
            return tb
