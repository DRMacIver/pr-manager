#!/bin/bash
# Wrapper around gh that authenticates via a bind-mounted token file.
# This avoids putting tokens in the environment or gh's config store.

TOKEN_FILE="/mnt/credentials/github_token"

if [ -f "$TOKEN_FILE" ]; then
    GITHUB_TOKEN="$(cat "$TOKEN_FILE")"
    export GITHUB_TOKEN
fi

exec /usr/bin/gh "$@"
