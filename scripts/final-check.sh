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
ruff check claude_sandbox scripts tests
python -m mypy claude_sandbox scripts/*.py
shellcheck run.sh scripts/*.sh .githooks/*
python -m pytest -q -p no:cacheprovider -n auto
# Real-container tests (what smoke.sh runs, minus its live-GitHub group, which
# needs a logged-in gh), including the launch lifecycle in test_launch.py. They
# need a working incus: this repo's claude_sandbox_host.yaml requests nesting,
# so a sandbox launched with --allow-nesting (or --nesting) has one. Without it
# they are skipped, loudly: a green run then says nothing about them.
if command -v incus >/dev/null && incus info >/dev/null 2>&1; then
  CS_INTEGRATION=1 python -m pytest -q -p no:cacheprovider -m incus tests/integration
else
  echo "[final-check] WARNING: no usable incus, so the real-container tests did" \
    "NOT run (relaunch with --allow-nesting; this repo requests it in" \
    "claude_sandbox_host.yaml)" >&2
fi
