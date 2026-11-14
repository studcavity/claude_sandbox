#!/bin/bash

# Modify the below to match your github user & shielded email address~
github_user="studcavity"
git_email="353176155+studcavity@users.noreply.github.com"

if [ -z "${1:-}" ]; then
  echo "Usage: $0 repository [extra claude_sandbox.py flags, e.g. --tmux]"
  echo "       $0 --setup-token   (one-time: save a Claude login token)"
  exit 1
fi

set -euo pipefail

# Warn (don't block) if this checkout is behind its upstream, so we don't
# run stale scripts. Quietly skipped when offline or without an upstream.
check_for_updates() {
  local dir behind
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  git -C "${dir}" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1 || return 0
  GIT_TERMINAL_PROMPT=0 timeout 10 git -C "${dir}" fetch --quiet 2>/dev/null || {
    echo "[run.sh] note: couldn't check for updates (git fetch failed)" >&2
    return 0
  }
  behind="$(git -C "${dir}" rev-list --count 'HEAD..@{u}' 2>/dev/null)" || return 0
  if [ "${behind}" -gt 0 ]; then
    echo "[run.sh] NOTICE: this checkout is ${behind} commit(s) behind" \
         "$(git -C "${dir}" rev-parse --abbrev-ref '@{u}')." >&2
    git -C "${dir}" log --oneline 'HEAD..@{u}' | sed 's/^/[run.sh]   /' >&2
    echo "[run.sh] run 'git -C ${dir} pull' and re-run to use the latest version." >&2
  fi
}
check_for_updates

# gate operations by anticipated user; if we're accidentally someone we don't
# expect, bail. --setup-token never touches GitHub, so it skips this.
if [ "$1" != "--setup-token" ]; then
  who_am_i="$(gh auth status -a --json hosts --jq '.hosts."github.com"[0].login')"
  if [ "${who_am_i}" != "${github_user}" ]; then
    echo "authenticated as an unexpected user! expected: ${github_user}, got: ${who_am_i}"
    exit 1
  fi
fi

# install any prereqs
if [ ! -d venv ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt


common=(--image images:ubuntu/24.04 --cli incus)
if [ "$1" = "--setup-token" ]; then
  exec python3 claude_sandbox.py "${common[@]}" "$@"
fi

python3 claude_sandbox.py --repo "${github_user}/${1}" "${common[@]}" --git-name "${github_user}" --git-email "${git_email}" "${@:2}"
