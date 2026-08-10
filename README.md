# pr-manager

A terminal UI for managing GitHub pull requests across multiple repos using Claude Code agents.

**Fair warning:** This is a personal tool, vibecoded into existence in conversation with Claude. It was built for a userbase of one (me). It will probably break for you. It may do unexpected things to your repos. Use at your own risk.

## What it does

pr-manager polls your open GitHub PRs and shows their current state:

- **Rebase / CI status** — shows whether each PR is behind its base,
  waiting on checks, failing, or green.
- **Review status** — draft, approved, changes requested, comment
  activity at a glance.

pr-manager never modifies your PRs on its own. When you want it to
act, invoke the fix command explicitly (TUI `f` or
`pr-manager fix <url>`). The fix command runs a Claude agent that
rebases onto the PR's base, fixes failing CI, and re-attributes bot
commits, looping until CI is green.

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
git clone git@github.com:DRMacIver/pr-manager.git
cd pr-manager
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
| `c` | Open an interactive Claude session (resumes if one exists; stops a running fix first) |
| `f` | Fix selected PR (opens tmux window running `pr-manager fix`) |
| `y` | Copy selected PR's info to the clipboard (in the detail modal: copy the log) |
| `/` | Toggle the chat assistant panel |
| `s` | Settings (Claude permission mode, theme) |
| `a` | Add a repo |
| `r` | Remove a repo |
| `x` | Remove selected local branch from the list |
| `q` | Quit |

### Options

```
--poll-interval N   Polling interval in minutes (default: 5)
--headless          Log to stdout instead of running the TUI
```

### Chat assistant

`/` opens a chat panel with an AI assistant that can inspect and
control the running TUI by executing Python against an internal API
(list PRs, read agent logs, stop sessions, …). It calls the Anthropic
API directly, so it needs an `ANTHROPIC_API_KEY` (or
`ANTHROPIC_AUTH_TOKEN`) in the environment — the Claude Code login used
for the PR agents does not cover it. Note that the assistant executes
Python in the pr-manager process with no sandbox.

## How it works

Each managed repo has a **pristine clone** that is fetched from GitHub once per poll cycle. Working clones for individual PRs and branches are created locally from the pristine (fast, no network), with their remote set back to GitHub for pushing.

Claude agents run via the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/), with output streamed to per-PR log files. Commits authored by agents are recorded per PR (visible in the detail modal). Before any agent works on a PR, the working clone is reset to the remote branch, so agent work always starts from the PR's true state.

State is persisted in `~/.local/share/pr-manager/state.json`.

## License

MIT. Copyright (c) 2026 David R. MacIver.
