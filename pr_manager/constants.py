from pathlib import Path

BASE_DIR = Path("~/.local/share/pr-manager").expanduser()
REPOS_DIR = BASE_DIR / "repos"
LOGS_DIR = BASE_DIR / "logs"
STATE_PATH = BASE_DIR / "state.json"

SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# (icon, rich style) per status
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "idle":          ("○", "dim"),
    "pending":       ("◌", "cyan"),
    "rebasing":      ("◉", "yellow"),
    "fixing_ci":     ("◉", "yellow"),
    "green":         ("✓", "green"),
    "error":         ("✗", "red bold"),
    "human_changes": ("⚠", "blue"),
    "local":         ("◇", "magenta"),
}
