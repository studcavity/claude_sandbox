#!/bin/bash
# Real-container checks. Needs incus (or lxc) on this host.
#   ./smoke.sh            integration tests only
#   ./smoke.sh REPO       also a real launch against REPO (owner/name), whose
#                         claude_sandbox_deps.yaml should have apt, binaries
#                         and scripts entries; runs the session with --rebuild
#                         and checks the install by running `true` in it.
# Run before pushing changes to container, image or deps code.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements-dev.txt
CS_INTEGRATION=1 python -m pytest -m incus -p no:cacheprovider tests/integration
if [ -n "${1:-}" ]; then
  echo "[smoke] real launch against $1 (with --rebuild)"
  ./run.sh "$1" --rebuild --shell <<<'exit'
fi
echo "[smoke] OK. Record 'Verified: ./smoke.sh' in your commit message."
