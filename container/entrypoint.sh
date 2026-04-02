#!/bin/bash
set -euo pipefail

# Fix ownership of home directory if it was just created by Docker as root.
if [ ! -w "$HOME" ]; then
    sudo chown -R dev:dev "$HOME"
fi

export CLAUDE_CONFIG_DIR="$HOME/.claude"

# ── SSH keys (mounted read-only, need to copy for correct permissions) ───────

if [ -d /mnt/host-ssh ]; then
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    cp /mnt/host-ssh/* ~/.ssh/ 2>/dev/null || true
    chmod 600 ~/.ssh/id_* 2>/dev/null || true
    chmod 644 ~/.ssh/*.pub 2>/dev/null || true
    if ! grep -q github.com ~/.ssh/known_hosts 2>/dev/null; then
        ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
    fi
fi

# ── GitHub CLI auth (mounted read-only, copy to writable location) ───────────

if [ -d /mnt/host-gh-config ] && [ ! -d ~/.config/gh ]; then
    mkdir -p ~/.config/gh
    cp -r /mnt/host-gh-config/* ~/.config/gh/ 2>/dev/null || true
fi

# ── Git config ───────────────────────────────────────────────────────────────

if [ -f /mnt/host-gitconfig ] && [ ! -f ~/.gitconfig ]; then
    cp /mnt/host-gitconfig ~/.gitconfig
fi

# ── Claude Code credentials ──────────────────────────────────────────────────
# Follows the same pattern as drmaciver-project:
# - /mnt/claude-credentials/claude-keychain.json -> ~/.claude/.credentials.json
# - /mnt/claude-credentials/claude-config.json   -> ~/.claude/.claude.json
# Uses a marker file to avoid re-copying on subsequent starts.

MARKER="$HOME/.claude-credentials-copied"

_needs_copy() {
    [ ! -f "$MARKER" ] && return 0
    [ ! -f "$CLAUDE_CONFIG_DIR/.credentials.json" ] && rm -f "$MARKER" && return 0
    [ ! -f "$CLAUDE_CONFIG_DIR/.claude.json" ] && rm -f "$MARKER" && return 0
    return 1
}

if [ -d /mnt/claude-credentials ] && _needs_copy; then
    mkdir -p "$CLAUDE_CONFIG_DIR"

    if [ -f /mnt/claude-credentials/claude-keychain.json ]; then
        cp /mnt/claude-credentials/claude-keychain.json "$CLAUDE_CONFIG_DIR/.credentials.json"
        chmod 600 "$CLAUDE_CONFIG_DIR/.credentials.json"
    fi

    if [ -f /mnt/claude-credentials/claude-config.json ]; then
        cp /mnt/claude-credentials/claude-config.json "$CLAUDE_CONFIG_DIR/.claude.json"
        chmod 600 "$CLAUDE_CONFIG_DIR/.claude.json"
    fi

    touch "$MARKER"
fi

exec "$@"
