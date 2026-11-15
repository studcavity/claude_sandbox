#!/bin/bash
# Thin shim: everything (venv setup, update check, gh user gate, defaults) lives
# in the claude_sandbox package (claude_sandbox/__main__.py). Builtins only, so the one process that starts is python.
# Usage: ./run.sh <repository> [flags]   |   ./run.sh --setup-token
set -euo pipefail
d=${BASH_SOURCE[0]%/*}
[[ $d == "${BASH_SOURCE[0]}" ]] && d=.
exec python3 "$d/claude_sandbox/__main__.py" "$@"
