#!/bin/bash
set -euo pipefail

# ── Credentials ──────────────────────────────────────────────────────────────
# Mounted at /mnt/credentials by the container manager.

if [ -d /mnt/credentials ]; then
    # Claude Code credentials
    mkdir -p ~/.claude
    if [ -f /mnt/credentials/claude-keychain.json ]; then
        cp /mnt/credentials/claude-keychain.json ~/.claude/credentials.json
        chmod 600 ~/.claude/credentials.json
    fi
    if [ -f /mnt/credentials/claude-config.json ]; then
        cp /mnt/credentials/claude-config.json ~/.claude/.claude.json
        chmod 600 ~/.claude/.claude.json
    fi
fi

# ── SSH keys ─────────────────────────────────────────────────────────────────

if [ -d /mnt/ssh-keys ]; then
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    cp /mnt/ssh-keys/* ~/.ssh/ 2>/dev/null || true
    chmod 600 ~/.ssh/* 2>/dev/null || true
    # Auto-accept GitHub host key
    if ! grep -q github.com ~/.ssh/known_hosts 2>/dev/null; then
        ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
    fi
fi

# ── Git config ───────────────────────────────────────────────────────────────

if [ -f /mnt/credentials/gitconfig ]; then
    cp /mnt/credentials/gitconfig ~/.gitconfig
fi

# ── GitHub token (for gh CLI) ────────────────────────────────────────────────
# gh-auth-wrapper reads the token fresh on each invocation.
# Set up an alias so `gh` uses the wrapper.

if [ -f /mnt/credentials/github_token ]; then
    git config --global alias.gh '!gh-auth-wrapper'
fi

exec "$@"
