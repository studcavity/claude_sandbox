#!/bin/bash
set -euo pipefail

# Modify the below to match your github user & shielded email address~
github_user="studcavity"
git_email="353176155+studcavity@users.noreply.github.com"

# Everything below (venv, requirements, scripts) lives next to this file.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --no-update-check is ours, not claude_sandbox.py's: strip it from anywhere
# in the args. Without it we offer to `git pull` when this checkout is behind.
orig_args=("$@")
check_updates=1
args=()
for a in "$@"; do
  if [ "$a" = "--no-update-check" ]; then check_updates=; else args+=("$a"); fi
done
set -- ${args[@]+"${args[@]}"}

if [ -z "${1:-}" ]; then
  echo "Usage: $0 repository [extra claude_sandbox.py flags, e.g. --tmux]"
  echo "       $0 --setup-token   (one-time: save a Claude login token)"
  exit 1
fi

# shellcheck source=update_check.sh
source "${script_dir}/update_check.sh"
if [ -n "${check_updates}" ]; then
  check_for_updates "${script_dir}" "${BASH_SOURCE[0]}" "${orig_args[@]}"
fi

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
if [ ! -d "${script_dir}/venv" ]; then
  python3 -m venv "${script_dir}/venv"
fi
# shellcheck source=/dev/null
source "${script_dir}/venv/bin/activate"
pip install -q -r "${script_dir}/requirements.txt"


common=(--image images:ubuntu/24.04 --cli incus)
if [ "$1" = "--setup-token" ]; then
  exec python3 "${script_dir}/claude_sandbox.py" "${common[@]}" "$@"
fi

exec python3 "${script_dir}/claude_sandbox.py" --repo "${github_user}/${1}" "${common[@]}" --git-name "${github_user}" --git-email "${git_email}" "${@:2}"
