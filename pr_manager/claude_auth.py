"""Detect and prompt for Claude Code login.

On Linux (i.e. inside the dev container), Claude Code writes OAuth tokens
to ~/.claude/.credentials.json. If that file is absent when pr-manager
starts, open a new tmux window running `claude` in a scratch workspace
so the user can authenticate, then block startup until the credentials
file appears.

On other platforms (notably macOS, where tokens live in the Keychain)
this check is skipped.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
LOGIN_WORKSPACE = Path.home() / ".cache" / "pr-manager" / "claude-login-workspace"


def is_logged_in() -> bool:
    if not CREDENTIALS_PATH.exists():
        return False
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
    except (OSError, ValueError):
        return False
    return bool(data)


def ensure_logged_in() -> None:
    if platform.system() != "Linux" or is_logged_in():
        return

    if not os.environ.get("TMUX"):
        print(
            "Claude Code is not logged in. Start pr-manager inside tmux "
            "(or run `claude` manually and log in) before retrying.",
            file=sys.stderr,
        )
        sys.exit(1)

    LOGIN_WORKSPACE.mkdir(parents=True, exist_ok=True)
    # `claude; exec bash` keeps the window alive if login is aborted, so
    # the user can retry without having to reopen it.
    shell_cmd = (
        'echo "Log in to Claude Code here, then switch back to pr-manager '
        '(Ctrl-b p)."; echo; claude; exec bash'
    )
    subprocess.run(
        [
            "tmux", "new-window",
            "-n", "claude-login",
            "-c", str(LOGIN_WORKSPACE),
            "sh", "-c", shell_cmd,
        ],
        check=True,
    )

    print("Claude Code is not logged in.", flush=True)
    print("Opened 'claude-login' tmux window - complete login there.", flush=True)
    print("Waiting for credentials...", flush=True)

    while not is_logged_in():
        time.sleep(1)

    print("Login detected. Starting pr-manager.", flush=True)
