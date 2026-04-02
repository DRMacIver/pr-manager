# pr-manager

A terminal UI for managing GitHub pull requests across multiple repos using Claude Code agents.

**Fair warning:** This is a personal tool, vibecoded into existence over the course of a single conversation with Claude. It was built for a userbase of one (me). It will probably break for you. It may do unexpected things to your repos. It has no tests. Use at your own risk.

## What it does

pr-manager polls your open GitHub PRs and automatically:

- **Rebases** branches that are behind main (via a Claude agent that handles conflicts)
- **Fixes failing CI** by having a Claude agent examine failures and commit fixes
- **Reattributes bot commits** when a GitHub bot push doesn't trigger Actions (amends the commit author and force-pushes to restart CI)
- **Tracks check status** — shows whether checks are green, pending, failing, or absent

It also provides shortcuts for interactive work:

- **Create new branches** from main with a Claude session already open
- **Open Claude sessions** on any PR or local branch, resuming the most recent session
- **View agent logs** in real time
- **Open PRs in your browser**
- **See review status** (draft, approved, changes requested) and comment activity at a glance

Everything runs inside tmux (auto-launched if needed) so you can have multiple Claude sessions and log tails open in separate windows.

## Requirements

- [uv](https://github.com/astral-sh/uv)
- [gh](https://cli.github.com/) (GitHub CLI, authenticated)
- [tmux](https://github.com/tmux/tmux)
- [Claude Code](https://claude.ai/claude-code) CLI (`claude`)
- Git with SSH access to your repos

## Setup

```sh
git clone <this-repo>
cd tooling
uv sync
```

## Usage

```sh
# Add repos to manage
uv run pr-manager add owner/repo

# Start the TUI (auto-launches tmux if not already inside one)
uv run pr-manager run

# Or run headless for debugging (logs to stdout, no TUI/tmux)
uv run pr-manager run --headless

# List managed repos
uv run pr-manager list

# Remove a repo
uv run pr-manager remove owner/repo
```

### TUI keybindings

| Key | Action |
|-----|--------|
| `b` | Open selected PR in browser |
| `d` | Show PR detail modal (state, session, log tail) |
| `n` | Create a new branch (prompts for repo + name, opens Claude) |
| `o` | Open a terminal in the PR's working directory |
| `v` | View agent log (tail -f in a new tmux window) |
| `c` | Open an interactive Claude session (resumes if one exists) |
| `s` | Settings (Claude permission mode) |
| `a` | Add a repo |
| `r` | Remove a repo |
| `q` | Quit |

### Options

```
--poll-interval N   Polling interval in minutes (default: 5)
--recent-minutes N  Skip PRs with human commits in the last N minutes (default: 30)
--headless          Log to stdout instead of running the TUI
```

## How it works

Each managed repo has a **pristine clone** that is fetched from GitHub once per poll cycle. Working clones for individual PRs and branches are created locally from the pristine (fast, no network), with their remote set back to GitHub for pushing.

Claude agents run via the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/), with output streamed to per-PR log files. The tool tracks which commits it has authored so it can distinguish its own changes from human pushes and avoid interfering with active work.

State is persisted in `~/.local/share/pr-manager/state.json`.

## License

MIT. Copyright (c) 2026 David R. MacIver.
