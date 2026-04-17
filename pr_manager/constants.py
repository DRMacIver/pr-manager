from pathlib import Path

BASE_DIR = Path("~/.local/share/pr-manager").expanduser()
REPOS_DIR = BASE_DIR / "repos"
LOGS_DIR = BASE_DIR / "logs"
STATE_PATH = BASE_DIR / "state.json"

SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# (icon, rich style) per status
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "idle":      ("○", "dim"),
    "pending":   ("◌", "cyan"),
    "behind":    ("↓", "yellow"),
    "no_checks": ("·", "dim"),
    "failing":   ("✗", "red bold"),
    "fixing":    ("◉", "yellow"),
    "green":     ("✓", "green"),
    "error":     ("!", "red bold"),
    "local":     ("◇", "magenta"),
}
