#!/bin/bash
# The sandbox's final check (`check:` in claude_sandbox_deps.yaml): the same
# lint and tests CI runs, run in the container when Claude exits so a failure
# can be fixed in the same session. Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements-dev.txt
python -m pyflakes ./*.py tests/
python -m mypy ./*.py
shellcheck run.sh smoke.sh final-check.sh .githooks/*
python -m pytest -q -p no:cacheprovider
