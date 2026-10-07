#!/usr/bin/env bash
# START_CONSOLE.sh - same as START.sh but console-only, never opens a window.
# Extra arguments are forwarded, e.g.: ./START_CONSOLE.sh --scan
exec "$(dirname "$0")/START.sh" --no-gui "$@"
