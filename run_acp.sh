#!/bin/bash
# ACP CLI wrapper — runs tsx from local node_modules, falls back to global tsx.
DIR="$(cd "$(dirname "$0")" && pwd)"
ACP_DIR="$DIR/openclaw/openclaw-acp"
cd "$ACP_DIR"
if [ -x "node_modules/.bin/tsx" ]; then
  exec node_modules/.bin/tsx bin/acp.ts "$@"
else
  exec tsx bin/acp.ts "$@"
fi
