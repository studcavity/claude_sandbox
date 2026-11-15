#!/bin/bash
# The sandbox's final check (`check:` in claude_sandbox_deps.yaml): the same
# lint and tests CI runs, run in the container when Claude exits so a failure
# can be fixed in the same session. Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements-dev.txt
python -m pyflakes claude_sandbox scripts/*.py tests/
python -m mypy claude_sandbox scripts/*.py
shellcheck run.sh scripts/*.sh .githooks/*
python -m pytest -q -p no:cacheprovider
# Real-container tests (what smoke.sh runs, minus its live-GitHub group, which
# needs a logged-in gh). They need a working incus, i.e. a sandbox launched
# with --nesting; without one they are skipped, never failed.
if command -v incus >/dev/null && incus info >/dev/null 2>&1; then
  CS_INTEGRATION=1 python -m pytest -q -p no:cacheprovider -m incus tests/integration
else
  echo "[final-check] no usable incus (launch with --nesting to include the" \
    "real-container tests); skipping them" >&2
fi
