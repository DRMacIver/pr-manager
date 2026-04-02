from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog

from .constants import STATUS_STYLE, SPINNER_CHARS
from .container import container_name_for, ensure_image_built, start_container
from .git import get_log_path, git_update_pristine, run_cmd
from .poll import poll_loop
from .state import CLAUDE_PERMISSION_MODES, PRDisplayInfo, PRState, Settings, StateManager


# ── Textual messages ─────────────────────────────────────────────────────────

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


# ── Add-repo modal ───────────────────────────────────────────────────────────

class NewBranchScreen(ModalScreen):
    DEFAULT_CSS = """
    NewBranchScreen {
        align: center middle;
    }
    #nb-dialog {
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $background 80%;
        background: $surface;
    }
    #nb-dialog Input { margin-bottom: 1; }
    #nb-buttons Button { margin-right: 1; }
    """

    def __init__(self, state_manager: StateManager, repos: list[str]) -> None:
        super().__init__()
        self._state_manager = state_manager
        self._repos = repos

    def compose(self) -> ComposeResult:
        from textual.widgets import Static
        with Vertical(id="nb-dialog"):
            yield Static("New branch")
            if len(self._repos) == 1:
                yield Input(value=self._repos[0], id="nb-repo")
            else:
                yield Input(placeholder="owner/repo", id="nb-repo")
            yield Input(placeholder="branch-name", id="nb-branch")
            with Horizontal(id="nb-buttons"):
                yield Button("Create", variant="primary", id="nb-create")
                yield Button("Cancel", id="nb-cancel")

    @on(Button.Pressed, "#nb-create")
    async def _create(self) -> None:
        repo = self.query_one("#nb-repo", Input).value.strip()
        branch = self.query_one("#nb-branch", Input).value.strip()
        if "/" not in repo:
            self.app.post_message(AppLogMessage("Invalid repo — expected owner/repo", "error"))
            return
        if not branch:
            self.app.post_message(AppLogMessage("Branch name required", "error"))
            return
        repos = await self._state_manager.get_repos()
        if repo not in repos:
            await self._state_manager.add_repo(repo)
        try:
            from .poll import _project_root
            await ensure_image_built(_project_root())
            await git_update_pristine(repo)
            cname = container_name_for(repo, branch)
            await start_container(repo, branch, branch, create_branch=True)
            await self._state_manager.add_local_branch(repo, branch)
            settings = await self._state_manager.get_settings()
            cmd = "claude"
            if settings.claude_permission_mode != "default":
                cmd += f" --permission-mode {settings.claude_permission_mode}"
            script = PRManagerApp._container_launch_script(cname, cmd)
            await run_cmd([
                "tmux", "new-window",
                "-n", f"new-{branch}",
                "bash", "-c", script,
            ], check=False)
            self.app.post_message(AppLogMessage(f"Created branch {branch} in {repo}", "info"))
        except Exception as e:
            self.app.post_message(AppLogMessage(f"Failed to create branch: {e}", "error"))
        self.dismiss()

    @on(Button.Pressed, "#nb-cancel")
    def _cancel(self) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()


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


# ── Settings modal ───────────────────────────────────────────────────────────

class SettingsScreen(ModalScreen):
    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-dialog {
        padding: 1 2;
        width: 64;
        height: auto;
        border: thick $background 80%;
        background: $surface;
    }
    #settings-dialog Static { margin-bottom: 1; }
    .setting-row { height: auto; margin-bottom: 1; }
    .setting-row Static { width: auto; margin-right: 1; }
    .setting-row Button { margin-right: 1; min-width: 0; }
    """

    def __init__(self, state_manager: StateManager, settings: Settings) -> None:
        super().__init__()
        self._state_manager = state_manager
        self._settings = settings

    def compose(self) -> ComposeResult:
        from textual.widgets import Static
        with Vertical(id="settings-dialog"):
            yield Static("Settings", id="settings-title")
            yield Static(f"Claude permission mode: [bold]{self._settings.claude_permission_mode}[/bold]",
                         id="perm-display")
            with Horizontal(classes="setting-row"):
                for mode in CLAUDE_PERMISSION_MODES:
                    yield Button(mode, id=f"perm-{mode}",
                                 variant="primary" if mode == self._settings.claude_permission_mode else "default")
            yield Button("Close", id="settings-close")

    @on(Button.Pressed)
    async def _on_button(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "settings-close":
            self.dismiss()
            return
        if btn_id.startswith("perm-"):
            mode = btn_id[5:]
            self._settings.claude_permission_mode = mode
            await self._state_manager.update_settings(self._settings)
            # Update display.
            from textual.widgets import Static
            self.query_one("#perm-display", Static).update(
                f"Claude permission mode: [bold]{mode}[/bold]"
            )
            # Update button variants.
            for m in CLAUDE_PERMISSION_MODES:
                btn = self.query_one(f"#perm-{m}", Button)
                btn.variant = "primary" if m == mode else "default"
            self.app.post_message(AppLogMessage(f"Permission mode set to: {mode}", "info"))

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()


# ── PR detail modal ──────────────────────────────────────────────────────────

class PRDetailScreen(ModalScreen):
    DEFAULT_CSS = """
    PRDetailScreen {
        align: center middle;
    }
    #detail-dialog {
        padding: 1 2;
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
    }
    #detail-header {
        height: auto;
        margin-bottom: 1;
    }
    #detail-log {
        height: 1fr;
        border-top: solid $primary-darken-2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("r", "refresh", "refresh"),
    ]

    def __init__(self, pr: PRDisplayInfo, pr_state: Optional[PRState]) -> None:
        super().__init__()
        self._pr = pr
        self._pr_state = pr_state

    def compose(self) -> ComposeResult:
        pr = self._pr
        st = self._pr_state
        icon, _ = STATUS_STYLE.get(pr.status, ("?", ""))

        with Vertical(id="detail-dialog"):
            from textual.widgets import Static
            lines = [
                f"PR #{pr.number} — {pr.title}",
                f"Repo: {pr.repo}  Branch: {pr.branch}  Age: {pr.age}",
                f"Status: {icon} {pr.status}",
            ]
            if pr.error_message:
                lines.append(f"Error: {pr.error_message}")
            if st:
                lines.append(f"Session: {st.session_id or 'none'}")
                lines.append(f"Our commits: {len(st.our_commits)}")
                lines.append(f"Last checked: {st.last_checked or 'never'}")
            yield Static("\n".join(lines), id="detail-header")
            yield RichLog(id="detail-log", highlight=True, markup=True)

    def on_mount(self) -> None:
        self._load_log()

    def _load_log(self) -> None:
        log_widget = self.query_one("#detail-log", RichLog)
        log_widget.clear()
        log_path = get_log_path(self._pr.repo, self._pr.number)
        if log_path.exists():
            text = log_path.read_text()
            # Show last 200 lines
            lines = text.splitlines()
            if len(lines) > 200:
                log_widget.write(f"[dim]... ({len(lines) - 200} earlier lines omitted) ...[/dim]")
                lines = lines[-200:]
            for line in lines:
                log_widget.write(line)
        else:
            log_widget.write("[dim]No agent log yet.[/dim]")

    def action_refresh(self) -> None:
        self._load_log()

    async def action_dismiss(self, result=None) -> None:
        self.dismiss()


# ── TUI app adapter ─────────────────────────────────────────────────────────
# Bridges the PollHost protocol to Textual messages.

class TuiPollHost:
    """Adapter so poll_loop can drive the Textual app."""

    def __init__(self, app: PRManagerApp) -> None:
        self._app = app
        self._active_tasks: dict[tuple[str, int], asyncio.Task] = app._active_tasks

    def on_log(self, text: str, level: str) -> None:
        self._app.post_message(AppLogMessage(text, level))

    def on_status_update(self, repo: str, pr_number: int, status: str, error: Optional[str]) -> None:
        self._app.post_message(PrStatusUpdate(repo, pr_number, status, error))

    def on_pr_list(self, prs: list[PRDisplayInfo]) -> None:
        self._app.post_message(PrListUpdate(prs))


# ── Main TUI application ────────────────────────────────────────────────────

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
        Binding("b", "open_browser", "browser"),
        Binding("d", "detail", "detail"),
        Binding("n", "new_branch", "new branch"),
        Binding("o", "open_terminal", "terminal"),
        Binding("v", "view_agent", "view agent"),
        Binding("c", "open_claude_session", "claude session"),
        Binding("s", "settings", "settings"),
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
        table.add_columns("PR#", "Repo", "Branch", "Status", "Review", "Activity", "Age")
        host = TuiPollHost(self)
        self._poll_task = asyncio.create_task(
            poll_loop(host, self._state_manager, self._poll_interval, self._recent_minutes)
        )
        self.set_interval(0.12, self._tick_spinner)

    # ── Spinner ──────────────────────────────────────────────────────────

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_CHARS)
        self._refresh_table()

    # ── Table rendering ──────────────────────────────────────────────────

    REVIEW_STYLES: dict[str, tuple[str, str]] = {
        "draft":            ("✎", "dim"),
        "approved":         ("✔", "green"),
        "changes requested":("✖", "red"),
        "review needed":    ("⊘", "yellow"),
        "in review":        ("◎", "cyan"),
    }

    def _format_review(self, review_status: str) -> Text:
        if not review_status:
            return Text("")
        icon, style = self.REVIEW_STYLES.get(review_status, ("", "dim"))
        return Text(f"{icon} {review_status}", style=style)

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
                str(pr.number) if pr.number else "—",
                pr.repo,
                pr.branch,
                self._format_status(pr.status, pr.is_active),
                self._format_review(pr.review_status),
                pr.activity,
                pr.age,
                key=f"{pr.repo}:{pr.number or pr.branch}",
            )
        try:
            if self._display_prs:
                table.move_cursor(row=min(saved_row, len(self._display_prs) - 1))
        except Exception:
            pass

    # ── Message handlers ─────────────────────────────────────────────────

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

    # ── Selected PR helper ───────────────────────────────────────────────

    def _get_selected_pr(self) -> Optional[PRDisplayInfo]:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._display_prs):
            return self._display_prs[row]
        return None

    @staticmethod
    def _container_id(pr: PRDisplayInfo) -> str:
        """Return the container identifier for a PR or local branch."""
        return str(pr.number) if pr.number else pr.branch

    @staticmethod
    def _container_launch_script(container_name: str, cmd: str) -> str:
        """Shell script that ensures the container is running, waits for readiness, then execs."""
        return (
            f'CNAME="{container_name}"\n'
            f'# Start the container if it exists but is stopped.\n'
            f'STATE=$(docker inspect -f "{{{{.State.Running}}}}" "$CNAME" 2>/dev/null)\n'
            f'if [ "$STATE" = "false" ]; then\n'
            f'  echo "Starting stopped container $CNAME..."\n'
            f'  docker start "$CNAME"\n'
            f'fi\n'
            f'echo "Waiting for container $CNAME to be ready..."\n'
            f'docker logs -f "$CNAME" 2>&1 &\n'
            f'LOG_PID=$!\n'
            f'while ! docker exec "$CNAME" test -f /tmp/.ready 2>/dev/null; do\n'
            f'  if ! docker inspect "$CNAME" >/dev/null 2>&1; then\n'
            f'    echo "ERROR: Container $CNAME does not exist."\n'
            f'    echo "Press enter to close..."\n'
            f'    read\n'
            f'    exit 1\n'
            f'  fi\n'
            f'  sleep 0.5\n'
            f'done\n'
            f'kill $LOG_PID 2>/dev/null\n'
            f'echo "Container ready."\n'
            f'docker exec -it -w /home/dev/repo "$CNAME" {cmd}\n'
            f'EXIT=$?\n'
            f'if [ $EXIT -ne 0 ]; then\n'
            f'  echo "Command exited with code $EXIT"\n'
            f'  echo "Press enter to close..."\n'
            f'  read\n'
            f'fi\n'
        )

    # ── Key actions ──────────────────────────────────────────────────────

    def _check_tmux(self) -> bool:
        if not os.environ.get("TMUX"):
            self.post_message(AppLogMessage(
                "Not inside a tmux session — run pr-manager inside tmux to use window actions",
                "warn",
            ))
            return False
        return True

    async def run_action(self, action, default_namespace=None, namespaces=None) -> bool:
        """Override to catch and log errors from all actions."""
        try:
            return await super().run_action(action, default_namespace, namespaces)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.post_message(AppLogMessage(f"Action failed: {e}", "error"))
            self.post_message(AppLogMessage(tb, "error"))
            return False

    async def action_detail(self) -> None:
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        pr_state = await self._state_manager.get_pr_state(pr.repo, str(pr.number))
        await self.push_screen(PRDetailScreen(pr, pr_state))

    async def action_open_browser(self) -> None:
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        url = f"https://github.com/{pr.repo}/pull/{pr.number}"
        await run_cmd(["open", url], check=False)

    async def action_open_terminal(self) -> None:
        if not self._check_tmux():
            return
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        identifier = self._container_id(pr)
        cname = container_name_for(pr.repo, identifier)
        # Ensure container exists (fire-and-forget — tmux script handles waiting).
        asyncio.create_task(start_container(
            pr.repo, identifier, pr.branch, create_branch=(pr.number == 0),
        ))
        script = self._container_launch_script(cname, "bash")
        await run_cmd([
            "tmux", "new-window", "-n", f"term-{pr.number or pr.branch}",
            "bash", "-c", script,
        ], check=False)

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
        await run_cmd([
            "tmux", "new-window",
            "-n", f"log-{pr.number or pr.branch}",
            f"tail -f {log_path}",
        ], check=False)
        self.post_message(AppLogMessage(
            f"Watching agent log (tail -f {log_path})", "info"
        ))

    async def action_open_claude_session(self) -> None:
        if not self._check_tmux():
            return
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        key = (pr.repo, pr.number)
        task = self._active_tasks.get(key)
        if task and not task.done():
            task.cancel()
            self.post_message(AppLogMessage(
                f"Interrupted automated agent for PR #{pr.number}", "warn"
            ))
        identifier = self._container_id(pr)
        cname = container_name_for(pr.repo, identifier)
        asyncio.create_task(start_container(
            pr.repo, identifier, pr.branch, create_branch=(pr.number == 0),
        ))
        pr_state = await self._state_manager.get_pr_state(pr.repo, str(pr.number))
        settings = await self._state_manager.get_settings()
        claude_cmd = "claude"
        if pr_state and pr_state.session_id:
            claude_cmd += f" --resume {pr_state.session_id}"
        if settings.claude_permission_mode != "default":
            claude_cmd += f" --permission-mode {settings.claude_permission_mode}"
        script = self._container_launch_script(cname, claude_cmd)
        await run_cmd([
            "tmux", "new-window", "-n", f"claude-{pr.number or pr.branch}",
            "bash", "-c", script,
        ], check=False)

    async def action_new_branch(self) -> None:
        if not self._check_tmux():
            return
        repos = await self._state_manager.get_repos()
        await self.push_screen(NewBranchScreen(self._state_manager, repos))

    async def action_settings(self) -> None:
        settings = await self._state_manager.get_settings()
        await self.push_screen(SettingsScreen(self._state_manager, settings))

    async def action_add_repo(self) -> None:
        await self.push_screen(AddRepoScreen(self._state_manager))

    async def action_remove_repo(self) -> None:
        pr = self._get_selected_pr()
        if not pr:
            self.post_message(AppLogMessage("No PR selected", "warn"))
            return
        await self._state_manager.remove_repo(pr.repo)
        self._display_prs = [p for p in self._display_prs if p.repo != pr.repo]
        self._refresh_table()
        self.post_message(AppLogMessage(f"Removed repo: {pr.repo}", "info"))
