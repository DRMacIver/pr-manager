#!/bin/bash
set -euo pipefail

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

# ~/.claude is bind-mounted directly from the host.

exec "$@"
