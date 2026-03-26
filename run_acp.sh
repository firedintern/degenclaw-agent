#!/bin/bash
# Wrapper so bot.py can call acp without relying on npm link / global PATH
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/openclaw/openclaw-acp"
exec node_modules/.bin/tsx bin/acp.ts "$@"
