#!/bin/bash
# ACP CLI wrapper — runs tsx directly from local node_modules.
# This avoids relying on `npm link` which is unreliable on Railway/CI.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/openclaw/openclaw-acp"
exec node_modules/.bin/tsx bin/acp.ts "$@"
