#!/bin/bash

# Modify the below to match your github user & shielded email address~
github_user="studcavity"
git_email="353176155+studcavity@users.noreply.github.com"

if [ -z "${1:-}" ]; then
  echo "Usage: $0 repository [extra claude_sandbox.py flags, e.g. --tmux]"
  echo "       $0 --setup-token   (one-time: save a Claude login token)"
  exit 1
fi

# gate operations by anticipated user; if we're accidentally someone we don't
# expect, bail.
set -euo pipefail
who_am_i="$(gh auth status -a --json hosts --jq '.hosts."github.com"[0].login')"
if [ "${who_am_i}" != "${github_user}" ]; then
  echo "authenticated as an unexpected user! expected: ${github_user}, got: ${who_am_i}"
  exit 1
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
